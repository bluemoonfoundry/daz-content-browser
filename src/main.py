import argparse
import json
import logging
import os
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
from utilities import open_daz_product
import uvicorn

from managers.managers import chroma_db_manager, sqlite_db

def load_command(args):
    """Loads data from DAZ Postgres to SQLite and ChromaDB."""
    from managers.postgres_db_manager import main as load_dazdb_content

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

        load_dazdb_content(args, on_progress=on_progress)

def morphs_index_command(args):
    """Indexes .dsf morph files from a DAZ library into morph_index.db, morph_cache/, and ChromaDB."""
    from managers.morph_index_manager import MorphIndexManager
    from managers.morph_transpiler import index_library, embed_and_store_morphs
    from managers.chroma_db_manager import ChromaDbManager

    library_path = args.library_path or os.environ.get("MORPH_LIBRARY_PATH")
    if not library_path:
        print("Error: --library-path is required (or set MORPH_LIBRARY_PATH).", file=sys.stderr)
        sys.exit(1)

    morph_db_path = os.environ.get("MORPH_INDEX_DB_PATH", "morph_index.db")
    tmb_output_dir = os.environ.get("MORPH_CACHE_PATH", "morph_cache")
    chroma_path = os.environ.get("CHROMA_PATH", "chroma_db")

    morph_index_manager = MorphIndexManager(morph_db_path)
    chroma_manager = ChromaDbManager(chroma_path, "morphs")
    if args.force:
        chroma_manager.reset_collection()

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
        embed_and_store_morphs(morph_index_manager, chroma_manager, summary["new_guids"], on_progress=on_progress)

    print(f"Scanned: {summary['scanned']}, Ingested: {summary['ingested']}, "
          f"Skipped (no deltas): {summary['skipped_no_deltas']}, "
          f"Skipped (unchanged): {summary['skipped_unchanged']}, Errors: {summary['errors']}")

def query_command(args):
    """Submits a query to the ChromaDB and prints the formatted results."""
    print("Starting query command...")

    # The search function returns a dictionary with 'total_hits', 'results', etc.
    response = chroma_db_manager.search(
        prompt=args.prompt,
        tags=args.tags,
        limit=args.limit,
        score_threshold=args.score,
        sort_by=args.sort_by,
        sort_order=args.sort_order,
    )

    if not response or not response.get("results"):
        print("\n--- No results found for your query. ---")
        return
    
    if args.format == 'json':
        print_json(response)
    elif args.format == 'table':
        print_table(response)
    else: # Default to 'pretty'
        print_pretty(response)

def stats_command(args):
    """Gathers and prints statistics about the ChromaDB collection."""

    print("Gathering statistics from the database...")
    stats = chroma_db_manager.get_db_stats()
    last_update = sqlite_db.get_last_updated()
    filter_values = sqlite_db.get_filter_values()

    print("\n--- ChromaDB Collection Stats ---")
    print(f"Total Documents Indexed: {stats['total_docs']}")
    print(f"Last Document Update:    {last_update}")
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
        print(f"--- Embedding model: {args.model} ---")
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
            "query", help="Query the ChromaDB vector store."
        ),
        "stats": subparsers.add_parser(
            "stats", help="Display stats about the ChromaDB collection."
        ),
        "server": subparsers.add_parser("server", help="Run the FastAPI web server."),
        "load": subparsers.add_parser("load", help="Load data from DAZ Postgres to SQLite and ChromaDB."),
        "openproduct": subparsers.add_parser(
            "openproduct",
            help="Open a named DAZ product in DAZ Studio's Content Library Pane",
        ),

    }

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

    parsers["load"].add_argument('--force', action='store_true', help="Force a complete rebuild of the SQLite database (implies --all).")
    parsers["load"].add_argument('--all', action='store_true', help="Process all products from Postgres, not just new ones.")
    parsers["load"].add_argument('--limit', type=int, help="Process only a limited number of products. Ideal for testing.")
    parsers["load"].add_argument('--phase', type=str, choices=['test', 'etl', 'embed', 'all'], default='all', help="Run only a specific phase: 'etl', 'embed', or both if omitted.")


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
