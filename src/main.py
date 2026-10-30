import argparse
import json
import logging
import os
import shutil
import sys

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

from output_formatters import print_pretty, print_json, print_table
from security import is_loopback_bind
from utilities import open_daz_product
import uvicorn

from managers.job_store import SUCCEEDED, JobConflict, public_view
from managers.managers import job_store


def _ensure_utf8_console_output() -> None:
    """Force stdout/stderr into UTF-8 mode before rendering rich.Progress bars.

    On Windows terminals running the legacy cp1252 codepage (the *default* on
    many Windows setups, not an edge case), rich's Progress/SpinnerColumn write
    Unicode characters (braille spinner glyphs, block-drawing bar characters,
    etc.) that cp1252 cannot encode. rich's legacy Windows console renderer
    doesn't handle the resulting UnicodeEncodeError gracefully, so
    Progress.__exit__ crashes during cleanup -- even after the underlying work
    has already completed successfully.

    Reconfiguring the streams to UTF-8 (an encoding that can represent every
    Unicode character) eliminates the failure at its source, without requiring
    the user to change any terminal/environment setting. ``errors="replace"``
    is a belt-and-suspenders fallback in case the reconfigured stream is later
    wrapped by something with a narrower encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Stream doesn't support reconfiguration (e.g. already detached,
            # or a non-standard stream substituted in tests); nothing to do.
            pass


def _run_index_job(kind: str, params: dict) -> None:
    """Runs an index job in this process through the shared job queue, with progress bars.

    Exits non-zero if another job (from the API or another terminal) is active, or if
    this one doesn't succeed.
    """
    from index_worker import run_inline

    _ensure_utf8_console_output()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    ) as progress:
        etl_task = progress.add_task("ETL       ", total=None)
        embed_task = progress.add_task("Embedding ", total=None, visible=False)

        def on_progress(stage: str, current: int, total: int, detail: str = ""):
            if stage == "etl":
                progress.update(
                    etl_task,
                    total=total,
                    completed=current,
                    description=f"ETL  {detail[:35]:<35}",
                )
            elif stage == "embed":
                progress.update(
                    embed_task,
                    visible=True,
                    total=total,
                    completed=current,
                    description="Embedding ",
                )

        try:
            job = run_inline(job_store, kind, params, source="cli", extra_progress=on_progress)
        except JobConflict as e:
            active = e.active or {}
            print(
                f"Error: index job {active.get('id')} is already {active.get('status')} "
                f"(started from {active.get('source')}). Wait for it, or `vab jobs cancel`.",
                file=sys.stderr,
            )
            sys.exit(1)

    if job is None:
        print("A background worker picked up the job; follow it with `vab jobs show`.")
        return
    print(f"Job {job['id']}: {job['status']} — {job['progress']}")
    if job["status"] != SUCCEEDED:
        sys.exit(1)


def load_job(args) -> tuple:
    """The job `vab load` runs: an incremental index, or with --force a refetching rebuild."""
    if args.force:
        conflicting = [flag for flag, set_ in (("--all", args.all), ("--phase", args.phase != "all"),
                                               ("--limit", args.limit is not None)) if set_]
        if conflicting:
            raise ValueError(
                f"`vab load --force` can't be combined with {', '.join(conflicting)}: --force always "
                "re-fetches every product and rebuilds the whole index.")
        params = {"refetch": True}
        if args.model:
            params["model"] = args.model
        return "rebuild", params
    params = {"all": args.all, "phase": args.phase, "limit": args.limit}
    if args.model:
        params["model"] = args.model
    return "index", params


def index_job(args) -> tuple:
    """The job `vab index rebuild|repair` runs."""
    if args.index_command == "rebuild":
        params = {"refetch": args.refetch}
        if args.model:
            params["model"] = args.model
        return "rebuild", params
    return "repair", {}


def load_command(args):
    """Reconciles the DAZ CMS into the catalog and embeds into the live index."""
    try:
        job = load_job(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    _run_index_job(*job)


def index_command(args):
    """`vab index rebuild|repair` — the explicit rebuild and recovery paths."""
    _run_index_job(*index_job(args))


def worker_command(args):
    """Runs queued index jobs (the API spawns `vab worker --drain` for each update)."""
    from index_worker import worker_command as run_worker

    run_worker(job_store, forever=not args.drain)


def _print_job(job: dict) -> None:
    print(json.dumps(public_view(job), indent=2))


def jobs_command(args):
    """Inspect and control index jobs: list, show, cancel, selftest."""
    if args.jobs_command == "list":
        for job in job_store.recent(args.limit):
            view = public_view(job)
            print(f"{view['id']}  {view['status']:<9}  {view['kind']:<8}  {view['source']:<4}  "
                  f"attempt {view['attempt']}/{view['max_attempts']}  {view['created_at']}  {view['progress']}")
    elif args.jobs_command == "show":
        job = job_store.get(args.job_id) if args.job_id else job_store.latest()
        if job is None:
            print("No such job." if args.job_id else "No jobs yet.", file=sys.stderr)
            sys.exit(1)
        _print_job(job)
    elif args.jobs_command == "cancel":
        job = job_store.request_cancel(args.job_id)
        if job is None:
            print("No active job to cancel.", file=sys.stderr)
            sys.exit(1)
        _print_job(job)
    elif args.jobs_command == "selftest":
        params = {"steps": args.steps, "delay": args.delay, "fail": args.fail}
        if args.background:
            from index_worker import spawn_worker
            job = job_store.enqueue("selftest", params, source="cli")
            spawn_worker(job_store)
            _print_job(job)
        else:
            _run_index_job("selftest", params)


def morphs_index_command(args):
    """Indexes .dsf morph files from a DAZ library into morph_index.db, morph_cache/, and ChromaDB."""
    from managers.morph_index_manager import MorphIndexManager
    from managers.morph_transpiler import index_library, embed_and_store_morphs, validate_library_root
    from managers.chroma_db_manager import ChromaDbManager

    library_path = args.library_path or os.environ.get("MORPH_LIBRARY_PATH")
    if not library_path:
        print("Error: --library-path is required (or set MORPH_LIBRARY_PATH).", file=sys.stderr)
        sys.exit(1)

    try:
        validate_library_root(library_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    morph_db_path = os.environ.get("MORPH_INDEX_DB_PATH", "morph_index.db")
    tmb_output_dir = os.environ.get("MORPH_CACHE_PATH", "morph_cache")
    chroma_path = os.environ.get("CHROMA_PATH", "chroma_db")

    _ensure_utf8_console_output()

    morph_index_manager = MorphIndexManager(morph_db_path)
    chroma_manager = ChromaDbManager(chroma_path, "morphs")
    if args.force:
        chroma_manager.reset_collection()
        shutil.rmtree(tmb_output_dir, ignore_errors=True)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
    ) as progress:
        scan_task = progress.add_task("Scanning  ", total=None)
        embed_task = progress.add_task("Embedding ", total=None, visible=False)

        def on_progress(stage, current, total, detail=""):
            if stage == "scan":
                progress.update(scan_task, completed=current, description=f"Scanning  {str(detail)[:35]:<35}")
            elif stage == "embed":
                progress.update(embed_task, visible=True, total=total, completed=current, description="Embedding ")

        summary = index_library(library_path, tmb_output_dir, morph_index_manager, force=args.force, on_progress=on_progress)

        # Embed every SQLite morph missing from Chroma, not just this run's
        # new_guids -- self-heals morphs left un-embedded by an interrupted
        # prior run (embedding only ever happens once, at the end of a full
        # walk, so a killed process strands its ingested-but-not-yet-embedded
        # morphs otherwise; content-hash matching then skips them forever).
        all_guids = set(morph_index_manager.get_all_guids())
        already_embedded = chroma_manager.get_all_ids()
        guids_to_embed = list(all_guids - already_embedded)

        embedded_count, failed_guids = embed_and_store_morphs(
            morph_index_manager, chroma_manager, guids_to_embed, on_progress=on_progress
        )

    if failed_guids:
        preview = ", ".join(failed_guids[:5])
        more = f" (showing first 5)" if len(failed_guids) > 5 else ""
        print(
            f"Warning: {len(failed_guids)} morph(s) failed to embed after retry and were not "
            f"stored in ChromaDB: {preview}{more}",
            file=sys.stderr,
        )

    print(f"Scanned: {summary['scanned']}, Ingested: {summary['ingested']}, "
          f"Skipped (no deltas): {summary['skipped_no_deltas']}, "
          f"Skipped (unchanged): {summary['skipped_unchanged']}, Errors: {summary['errors']}, "
          f"Embed failures: {len(failed_guids)}")

def query_command(args):
    """Searches the active index generation and prints the formatted results."""
    from catalog.active_index import SearchUnavailable, mcp_response
    from managers.managers import active_index, catalog

    if catalog.migration_error:
        print(f"Error: the catalog is unavailable: {catalog.migration_error}", file=sys.stderr)
        sys.exit(1)
    try:
        rows = active_index.search(
            catalog,
            args.prompt,
            max_results=500,
            category=args.categories,
            artists=args.artists,
            compatible_figures=args.compatible_figures,
            tags=args.tags,
        )
    except SearchUnavailable as e:
        print(f"Search unavailable ({e.reason}): {e.repair}", file=sys.stderr)
        sys.exit(1)
    response = mcp_response(rows, limit=args.limit, offset=0, score_threshold=args.score,
                            sort_by=args.sort_by, sort_order=args.sort_order)

    if not response["results"]:
        print("\n--- No results found for your query. ---")
        return

    if args.format == 'json':
        print_json(response)
    elif args.format == 'table':
        print_table(response)
    else: # Default to 'pretty'
        print_pretty(response)


def stats_command(args):
    """Prints the active index generation's state and the catalog's filter values."""
    from catalog.model_config import configured_model_id
    from catalog.status import index_status, rebuild_hint
    from managers.managers import catalog, generations

    if catalog.migration_error:
        print(f"Error: the catalog is unavailable: {catalog.migration_error}", file=sys.stderr)
        sys.exit(1)
    print("Gathering statistics from the database...")
    configured = configured_model_id()
    status = index_status(catalog, generations, configured, cms_reachable=False, pending_cms=None,
                          job_active=job_store.active() is not None)
    if status["error"]:
        print(f"Error: could not read the index status: {status['error']}", file=sys.stderr)
        sys.exit(1)
    last_update = catalog.get_last_updated()
    filter_values = catalog.get_filter_values()
    assets, vectors, active = status["assets"], status["vectors"], status["active_generation"]

    print("\n--- Index Stats ---")
    print(f"Products:                {assets['total']} ({assets['installed']} installed, "
          f"{assets['not_installed']} not installed, {assets['fetch_errors']} fetch errors)")
    if active:
        print(f"Active generation:       {active['id']} ({active['model_id']}, dim {active['model_dim']})")
        print(f"Total Documents Indexed: {vectors['current'] + vectors['stale']}")
        print(f"Vectors:                 {vectors['current']} current, {vectors['stale']} stale, "
              f"{vectors['missing']} missing, {vectors['error']} error, {vectors['orphan']} orphan")
    else:
        print("Active generation:       none")
    print(f"Last Document Update:    {last_update}")
    if status["rebuild_required"]:
        print(f"Rebuild required ({status['rebuild_reason']}): {rebuild_hint(configured)}")
    if status["resume_hint"]:
        print(status["resume_hint"])
    print("---------------------------------")

    for key, values in filter_values.items():
        display = values[:25]
        print(f"\n -- {key} ({len(display)} of {len(values)})")
        for v in display:
            print(f"  {v}")
        print("-------------------------------")


def server_command(args):
    """Runs the FastAPI web server."""
    if args.demo:
        print(
            "=" * 50
            + "\n==         STARTING SERVER IN DEMO MODE         ==\n"
            + "=" * 50
        )
        os.environ["APP_MODE"] = "demo"
    else:
        print("--- Starting server in Production Mode ---")
        os.environ["APP_MODE"] = "production"
    if args.model:
        os.environ["EMBEDDING_MODEL_ID"] = args.model
        os.environ["EMBEDDING_MODEL_OVERRIDE"] = args.model  # beats settings.json in configured_model_id()
        print(f"--- Embedding model: {args.model} ---")
    if not is_loopback_bind(args.host):
        print(
            f"WARNING: binding to {args.host} exposes the API beyond this machine. Requests are "
            "still rejected unless their Host is listed in VAB_ALLOWED_HOSTS."
        )
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.demo)

def main():
    """Main entry point for the CLI application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Visual Asset Browser Data Pipeline CLI"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Define commands
    parsers = {
        "query": subparsers.add_parser(
            "query", help="Search the active product index."
        ),
        "stats": subparsers.add_parser(
            "stats", help="Display stats about the product index."
        ),
        "server": subparsers.add_parser("server", help="Run the FastAPI web server."),
        "load": subparsers.add_parser(
            "load", help="Bring the index up to date with the DAZ CMS (adds, edits, removals) and embed changes."
        ),
        "openproduct": subparsers.add_parser(
            "openproduct",
            help="Open a named DAZ product in DAZ Studio's Content Library Pane",
        ),

    }

    index_parser = subparsers.add_parser("index", help="Rebuild or repair the product index.")
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)
    index_rebuild = index_subparsers.add_parser(
        "rebuild",
        help="Rebuild the vectors from SQLite into a new index generation beside the live one, then "
             "switch over. An interrupted rebuild resumes when run again.",
    )
    index_rebuild.add_argument(
        "--refetch", action="store_true",
        help="First re-fetch every product's store data (same as load --force). SQLite is never deleted.",
    )
    index_rebuild.add_argument(
        "--model", default=None, metavar="MODEL_ID",
        help="Embedding model for the new generation (default: the configured model).",
    )
    index_subparsers.add_parser(
        "repair", help="Quarantine the vector store if it won't open, then rebuild the index from SQLite "
                       "beside the live one."
    )
    index_parser.set_defaults(func=index_command)

    worker_parser = subparsers.add_parser("worker", help="Run queued index jobs.")
    worker_parser.add_argument("--drain", action="store_true", help="Exit once the queue is empty instead of polling.")
    worker_parser.set_defaults(func=worker_command)

    jobs_parser = subparsers.add_parser("jobs", help="Inspect and control index jobs.")
    jobs_subparsers = jobs_parser.add_subparsers(dest="jobs_command", required=True)
    jobs_list = jobs_subparsers.add_parser("list", help="Recent jobs, newest first.")
    jobs_list.add_argument("--limit", type=int, default=20)
    jobs_show = jobs_subparsers.add_parser("show", help="One job (default: the active or latest).")
    jobs_show.add_argument("job_id", nargs="?")
    jobs_cancel = jobs_subparsers.add_parser("cancel", help="Cancel a job (default: the active one).")
    jobs_cancel.add_argument("job_id", nargs="?")
    jobs_selftest = jobs_subparsers.add_parser("selftest", help="Run a no-op job to check the worker.")
    jobs_selftest.add_argument("--steps", type=int, default=3)
    jobs_selftest.add_argument("--delay", type=float, default=0.2)
    jobs_selftest.add_argument("--fail", action="store_true", help="Make the job fail, to check error reporting.")
    jobs_selftest.add_argument("--background", action="store_true", help="Queue it for a spawned worker instead of running inline.")
    jobs_parser.set_defaults(func=jobs_command)

    morphs_parser = subparsers.add_parser("morphs", help="Morph library ingest commands.")
    morphs_subparsers = morphs_parser.add_subparsers(dest="morphs_command", required=True)
    morphs_index_parser = morphs_subparsers.add_parser(
        "index", help="Index .dsf morph files into morph_index.db and ChromaDB."
    )
    morphs_index_parser.add_argument(
        "--library-path", type=str, default=None,
        help="Path to the DAZ content library root (containing data/). Falls back to MORPH_LIBRARY_PATH env var.",
    )
    morphs_index_parser.add_argument(
        "--force", action="store_true",
        help="Wipe morph_index.db, morph_cache/, and the morphs Chroma collection, then re-index everything.",
    )
    morphs_index_parser.set_defaults(func=morphs_index_command)

    parsers["query"].add_argument("prompt", help="The search prompt.")
    parsers["query"].add_argument(
        "--tags", nargs="*", help="List of tags to pre-filter results."
    )
    parsers["query"].add_argument(
        "--limit", type=int, default=5, help="Max number of results."
    )
    parsers["query"].add_argument(
        "--score", type=float, default=1.0, help="Maximum distance (lower is better)."
    )
    parsers["query"].add_argument(
        "--sort-by", default="relevance", help="Field to sort by ('relevance', 'name')."
    )
    parsers["query"].add_argument(
        "--sort-order", choices=["ascending", "descending"], default="descending"
    )
    parsers["query"].add_argument("--categories", nargs='*', help="List of categories to filter by.")
    parsers["query"].add_argument(
        "--format",
        choices=['pretty', 'json', 'table'],
        default='pretty',
        help="The output format for the results."
    )
    parsers["query"].add_argument("--artists", nargs='*', help="List of artists to filter by.")
    parsers["query"].add_argument("--compatible_figures", nargs='*', help="List of figures to filter by.")

    parsers["server"].add_argument("--host", default="127.0.0.1")
    parsers["server"].add_argument("--port", type=int, default=8000)
    parsers["server"].add_argument(
        "--demo", action="store_true", help="Run server in demo mode."
    )
    parsers["server"].add_argument(
        "--model", default="", metavar="MODEL_ID",
        help="HuggingFace model ID for embeddings (e.g. BAAI/bge-m3). "
             "Defaults to BAAI/bge-large-en-v1.5.",
    )

    parsers["load"].add_argument(
        '--force', action='store_true',
        help="Re-fetch every product's store data and build a new index generation beside the live one "
             "(same as `index rebuild --refetch`). Does NOT delete SQLite; an interrupted run resumes.",
    )
    parsers["load"].add_argument('--all', action='store_true', help="Re-fetch store data for every listed product, not just new or changed ones.")
    parsers["load"].add_argument('--limit', type=int, help="Process at most this many new/changed products (and embed at most this many). Ideal for testing.")
    parsers["load"].add_argument('--phase', type=str, choices=['etl', 'embed', 'all'], default='all', help="Run only a specific phase: 'etl' (reconcile the DAZ CMS), 'embed', or both if omitted.")
    parsers["load"].add_argument(
        '--model', default=None, metavar="MODEL_ID",
        help="Embedding model for the first index generation (or for --force's new generation). An "
             "existing index keeps its model; switch with `vab index rebuild --model`.",
    )


    parsers["openproduct"].add_argument(
        "--product", help="The name of the product to open in DAZ Studio."
    )

    # Set default functions
    func_map = {
        "query": query_command,
        "stats": stats_command,
        "server": server_command,
        "load": load_command,
        "openproduct": open_daz_product,
    }
    for cmd, sub_parser in parsers.items():
        sub_parser.set_defaults(func=func_map[cmd])

    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] == "help"):
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
