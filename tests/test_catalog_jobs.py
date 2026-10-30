"""Index / rebuild / repair jobs on the generation catalog, driven through the job queue."""
import types

import pytest

import index_worker
from catalog.builder import GenerationBuilder
from catalog.generations import GenerationStore
from catalog.store import WEB_SOURCE, CatalogStore
from catalog_fakes import FIGS, FakeScraper, cms_row, fake_embed, web_payload
from managers.chroma_db_manager import ChromaDbManager
from managers.job_store import CANCELLED, FAILED, SUCCEEDED, JobStore

SKUS = ["A1", "B2", "C3", "D4"]


@pytest.fixture
def job_env(tmp_path, monkeypatch, fake_embedder):
    from managers import managers as m

    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "fake/model-a")
    monkeypatch.setenv("BATCH_SIZE", "1")
    monkeypatch.setattr(index_worker, "_CANCEL_CHECK_SECONDS", 0)
    monkeypatch.setattr(index_worker, "_REOPEN_PAUSE_SECONDS", 0)

    db = str(tmp_path / "catalog.db")
    chroma_path = str(tmp_path / "chroma")
    catalog = CatalogStore(db)
    assert catalog.migration_error is None
    gens = GenerationStore(db)
    builder = GenerationBuilder(catalog, gens, chroma_path, "legacy")

    ns = types.SimpleNamespace(catalog=catalog, gens=gens, builder=builder,
                               chroma_path=chroma_path, cms=[cms_row(s) for s in SKUS],
                               scraper=FakeScraper(), store=JobStore(str(tmp_path / "jobs.db")))
    analyzer = types.SimpleNamespace(get_all_products=lambda: list(ns.cms))
    monkeypatch.setattr(m, "catalog", catalog)
    monkeypatch.setattr(m, "generations", gens)
    monkeypatch.setattr(m, "builder", builder)
    monkeypatch.setattr(m, "daz_pg_analyzer", analyzer)
    monkeypatch.setattr(index_worker, "_scraper", lambda ctx: ns.scraper)
    monkeypatch.setattr(index_worker, "_figure_names", lambda: FIGS)

    def run(kind, params=None, extra_progress=None):
        return index_worker.run_inline(ns.store, kind, params or {}, extra_progress=extra_progress)
    ns.run = run

    def ids(gen):
        return set(builder.collection(gen).get_all_ids())
    ns.ids = ids
    return ns


def test_index_job_reconciles_and_embeds_into_active(job_env):
    first = job_env.run("index")
    assert first["status"] == SUCCEEDED, first
    gen = job_env.gens.active()
    assert gen["model_id"] == "fake/model-a"
    assert job_env.ids(gen) == {f"daz:{s}" for s in SKUS}

    job_env.cms.append(cms_row("E5"))
    job_env.cms[0] = cms_row("A1", "Renamed A1", date="2025-02-02T00:00:00")
    job_env.scraper = FakeScraper()
    done = job_env.run("index")
    assert done["status"] == SUCCEEDED, done
    assert job_env.gens.active()["id"] == gen["id"]  # incremental: no new generation
    assert job_env.gens.building() is None
    assert sorted(job_env.scraper.scraped) == ["A1", "E5"]
    assert "daz:E5" in job_env.ids(gen)
    assert job_env.gens.pending(gen["id"]) == []
    assert "1 new" in done["progress"] and "1 changed" in done["progress"]
    assert "2 embedded" in done["progress"]


def test_index_job_embeds_with_active_model_never_rebuilds(job_env, monkeypatch):
    job_env.run("index")
    gen = job_env.gens.active()
    monkeypatch.setenv("EMBEDDING_MODEL_OVERRIDE", "fake/model-b")
    job_env.cms.append(cms_row("E5"))
    assert job_env.run("index", {"model": "fake/model-b"})["status"] == SUCCEEDED
    assert job_env.gens.active()["id"] == gen["id"]
    assert job_env.gens.building() is None
    assert job_env.gens.pending(gen["id"]) == []


def test_index_job_without_generation_builds_first_one(job_env):
    assert job_env.gens.active() is None
    done = job_env.run("index", {"phase": "all"})
    assert done["status"] == SUCCEEDED, done
    gen = job_env.gens.active()
    assert gen is not None and gen["collection_name"] == f"daz_products_g{gen['id']}"
    assert job_env.gens.vector_totals(gen["id"])["current"] == len(SKUS)


def test_index_job_phases(job_env):
    assert job_env.run("index", {"phase": "etl"})["status"] == SUCCEEDED
    assert job_env.catalog.count() == len(SKUS)
    assert job_env.gens.active() is None

    job_env.run("index", {"phase": "embed"})
    gen = job_env.gens.active()
    assert gen is not None and job_env.gens.pending(gen["id"]) == []


def test_index_job_without_cms_fails_unless_embed_only(job_env, monkeypatch):
    from managers import managers as m
    monkeypatch.setattr(m, "daz_pg_analyzer", None)
    done = job_env.run("index")
    assert done["status"] == FAILED and "DAZ CMS PostgreSQL is not available" in done["error"]
    assert job_env.run("index", {"phase": "embed"})["status"] == SUCCEEDED


def test_index_job_adopts_legacy_collection(job_env):
    job_env.run("index", {"phase": "etl"})
    legacy = ChromaDbManager(job_env.chroma_path, "legacy")
    legacy.collection.upsert(ids=["A1", "B2"],
                             embeddings=fake_embed(["a", "b"], model_id="fake/model-a").tolist(),
                             documents=["a", "b"])
    assert job_env.run("index", {"phase": "embed"})["status"] == SUCCEEDED
    gen = job_env.gens.active()
    assert gen["collection_name"] == "legacy"
    # Adopted vectors keep their bare SKU ids (no re-embedding); the rest are added.
    assert job_env.ids(gen) == {"A1", "B2", "daz:C3", "daz:D4"}
    assert job_env.gens.pending(gen["id"]) == []


def test_reconcile_aborted_fails_job_and_writes_nothing(job_env):
    job_env.run("index")
    job_env.cms.clear()
    done = job_env.run("index")
    assert done["status"] == FAILED and "refusing" in done["error"]
    assert job_env.catalog.count() == len(SKUS)


def test_rebuild_job_promotes_new_model(job_env):
    job_env.run("index")
    old = job_env.gens.active()
    done = job_env.run("rebuild", {"model": "fake/model-b"})
    assert done["status"] == SUCCEEDED, done
    gen = job_env.gens.active()
    assert gen["model_id"] == "fake/model-b" and gen["id"] != old["id"]
    assert job_env.gens.get(old["id"]) is None  # retired and cleaned up
    assert job_env.ids(gen) == {f"daz:{s}" for s in SKUS}


def test_rebuild_job_does_not_refetch(job_env):
    job_env.run("index")
    job_env.scraper = FakeScraper()
    assert job_env.run("rebuild", {"refetch": False})["status"] == SUCCEEDED
    assert job_env.scraper.calls == []


def test_cancelled_rebuild_discards_building_keeps_active(job_env, monkeypatch):
    job_env.run("index")
    active = job_env.gens.active()

    def cancel(stage, current, total, detail=""):
        if stage == "embed":
            job_env.store.request_cancel()

    done = job_env.run("rebuild", {"model": "fake/model-b"}, extra_progress=cancel)
    assert done["status"] == CANCELLED
    assert job_env.gens.building() is None
    assert job_env.gens.active()["id"] == active["id"]
    assert job_env.ids(active) == {f"daz:{s}" for s in SKUS}


def test_keyboard_interrupt_keeps_building_generation(job_env):
    job_env.run("index")
    active = job_env.gens.active()

    def interrupt(stage, current, total, detail=""):
        if stage == "embed" and current >= 1:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        job_env.run("rebuild", {"model": "fake/model-b"}, extra_progress=interrupt)
    building = job_env.gens.building()
    assert building is not None
    assert job_env.gens.active()["id"] == active["id"]
    job = job_env.store.latest()
    assert job["status"] == CANCELLED
    assert job["progress"] == "Interrupted — run the same command again to resume."

    done = job_env.run("rebuild", {"model": "fake/model-b"})  # resumes same generation id
    assert done["status"] == SUCCEEDED, done
    assert job_env.gens.active()["id"] == building["id"]
    assert job_env.gens.active()["model_id"] == "fake/model-b"


def test_force_resume_refetches_only_unrefreshed(job_env, monkeypatch):
    import catalog.reconcile as reconcile
    job_env.run("index")
    monkeypatch.setattr(reconcile, "SCRAPE_BATCH", 1)

    class Interrupting(FakeScraper):
        def __call__(self, skus):
            if self.calls:
                raise KeyboardInterrupt
            return super().__call__(skus)

    fresh = {s: web_payload(s, description=f"Fresh {s}.") for s in SKUS}
    job_env.scraper = Interrupting(pages=fresh)
    with pytest.raises(KeyboardInterrupt):
        job_env.run("rebuild", {"refetch": True})
    first = job_env.scraper.scraped
    assert len(first) == 1
    assert job_env.gens.building()["refetch_started_at"] is not None

    job_env.scraper = FakeScraper(pages=fresh)
    done = job_env.run("rebuild", {"refetch": True})
    assert done["status"] == SUCCEEDED, done
    assert sorted(job_env.scraper.scraped) == sorted(set(SKUS) - set(first))
    # Every asset's view reflects the refetched store data, including the one scraped
    # before the interruption.
    for sku in SKUS:
        assert job_env.catalog.get_by_sku(sku)["description"] == f"Fresh {sku}."


def test_rebuild_incomplete_fails_and_keeps_building(job_env, monkeypatch):
    import embedding_utils
    job_env.run("index")
    active = job_env.gens.active()

    def broken(texts, is_query=False, model_id=None):
        if model_id == "fake/model-b" and not isinstance(texts, str):
            raise RuntimeError("model exploded")
        return fake_embed(texts, is_query=is_query, model_id=model_id)

    monkeypatch.setattr(embedding_utils, "generate_embeddings", broken)
    done = job_env.run("rebuild", {"model": "fake/model-b"})
    assert done["status"] == FAILED
    # The hint resumes this generation; a plain `vab index rebuild` would discard it.
    assert "`vab index rebuild --model fake/model-b`" in done["error"]
    assert job_env.gens.building() is not None
    assert job_env.gens.active()["id"] == active["id"]


def test_jobs_refuse_when_migration_failed(job_env):
    job_env.catalog.migration_error = "schema v3 failed: disk full"
    for kind in ("index", "rebuild", "repair"):
        done = job_env.run(kind)
        assert done["status"] == FAILED
        assert "disk full" in done["error"]
    assert job_env.gens.active() is None


def test_repair_rebuilds_beside_active_without_resetting_it(job_env):
    job_env.run("index")
    old = job_env.gens.active()
    done = job_env.run("repair")
    assert done["status"] == SUCCEEDED, done
    gen = job_env.gens.active()
    assert gen["id"] != old["id"] and gen["model_id"] == old["model_id"]
    assert job_env.ids(gen) == {f"daz:{s}" for s in SKUS}
    assert len(job_env.scraper.calls) == 1  # only the first index run scraped


def test_repair_quarantines_an_unopenable_store(job_env, tmp_path):
    job_env.run("index")
    old = job_env.gens.active()
    # The store the active generation lived in no longer opens at all.
    broken = tmp_path / "broken_chroma"
    broken.write_text("not a chroma store", encoding="utf-8")
    job_env.builder.chroma_path = str(broken)
    job_env.builder._managers.clear()
    done = job_env.run("repair")
    assert done["status"] == SUCCEEDED, done
    assert "moved it to" in done["progress"]
    assert any(p.name.startswith("broken_chroma.corrupt-") for p in tmp_path.iterdir())
    gen = job_env.gens.active()
    assert gen["id"] != old["id"] and gen["model_id"] == old["model_id"]
    assert job_env.gens.vector_totals(gen["id"])["current"] == len(SKUS)
    # The old active's collection went with the quarantined store; cleanup tolerates that.
    assert job_env.gens.get(old["id"]) is None


def test_refetch_rebuild_without_cms_fails_before_starting(job_env, monkeypatch):
    from managers import managers as m
    job_env.run("index")
    monkeypatch.setattr(m, "daz_pg_analyzer", None)
    done = job_env.run("rebuild", {"refetch": True})
    assert done["status"] == FAILED and "DAZ CMS PostgreSQL is not available" in done["error"]
    assert job_env.gens.building() is None


@pytest.mark.parametrize("argv, kind, params", [
    (["load"], "index", {"all": False, "phase": "all", "limit": None}),
    (["load", "--all", "--phase", "etl", "--limit", "5", "--model", "fake/model-b"], "index",
     {"all": True, "phase": "etl", "limit": 5, "model": "fake/model-b"}),
    (["load", "--force"], "rebuild", {"refetch": True}),
    (["index", "rebuild"], "rebuild", {"refetch": False}),
    (["index", "rebuild", "--refetch", "--model", "fake/model-b"], "rebuild",
     {"refetch": True, "model": "fake/model-b"}),
    (["index", "repair"], "repair", {}),
])
def test_cli_maps_to_jobs(monkeypatch, argv, kind, params):
    import main
    ran = []
    monkeypatch.setattr(main, "_run_index_job", lambda k, p: ran.append((k, p)))
    monkeypatch.setattr("sys.argv", ["vab", *argv])
    main.main()
    assert ran == [(kind, params)]


def test_cli_load_has_no_test_phase(monkeypatch):
    import main
    monkeypatch.setattr("sys.argv", ["vab", "load", "--phase", "test"])
    with pytest.raises(SystemExit):
        main.main()


def test_cancelled_index_just_stops(job_env):
    job_env.run("index")
    active = job_env.gens.active()
    job_env.cms += [cms_row("E5"), cms_row("F6")]

    def cancel(stage, current, total, detail=""):
        if stage == "embed":
            job_env.store.request_cancel()

    done = job_env.run("index", extra_progress=cancel)
    assert done["status"] == CANCELLED
    assert job_env.gens.active()["id"] == active["id"]
    assert job_env.gens.building() is None
    assert job_env.gens.pending(active["id"]) == ["daz:F6"]  # partial work stays


def test_plain_rebuild_finishes_an_interrupted_refetch(job_env, monkeypatch):
    import catalog.reconcile as reconcile
    job_env.run("index")
    monkeypatch.setattr(reconcile, "SCRAPE_BATCH", 1)

    class Interrupting(FakeScraper):
        def __call__(self, skus):
            if self.calls:
                raise KeyboardInterrupt
            return super().__call__(skus)

    fresh = {s: web_payload(s, description=f"Fresh {s}.") for s in SKUS}
    job_env.scraper = Interrupting(pages=fresh)
    with pytest.raises(KeyboardInterrupt):
        job_env.run("rebuild", {"refetch": True})
    first = job_env.scraper.scraped
    building = job_env.gens.building()

    job_env.scraper = FakeScraper(pages=fresh)
    done = job_env.run("rebuild")  # no --refetch: the generation's marker decides
    assert done["status"] == SUCCEEDED, done
    assert job_env.gens.active()["id"] == building["id"]
    assert sorted(job_env.scraper.scraped) == sorted(set(SKUS) - set(first))
    for sku in SKUS:
        assert job_env.catalog.get_by_sku(sku)["description"] == f"Fresh {sku}."


def test_refetch_resume_hint_includes_refetch(job_env):
    gen = {"model_id": "fake/model-b", "refetch_started_at": "2026-01-01T00:00:00+00:00"}
    assert "`vab index rebuild --model fake/model-b --refetch`" in index_worker._resume_hint(gen)
    gen["refetch_started_at"] = None
    assert "`vab index rebuild --model fake/model-b`" in index_worker._resume_hint(gen)


def test_repair_retries_a_transient_open_failure(job_env, monkeypatch, tmp_path):
    import managers.chroma_db_manager as cdm
    job_env.run("index")
    answers = ["OSError: briefly locked", None]
    monkeypatch.setattr(cdm, "store_open_error", lambda path: answers.pop(0))
    done = job_env.run("repair")
    assert done["status"] == SUCCEEDED, done
    assert answers == []
    assert "moved it to" not in done["progress"]
    assert not any(".corrupt-" in p.name for p in tmp_path.iterdir())


@pytest.mark.parametrize("extra", [["--all"], ["--phase", "embed"], ["--limit", "5"]])
def test_cli_load_force_rejects_partial_flags(monkeypatch, capsys, extra):
    import main
    ran = []
    monkeypatch.setattr(main, "_run_index_job", lambda k, p: ran.append((k, p)))
    monkeypatch.setattr("sys.argv", ["vab", "load", "--force", *extra])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code != 0 and ran == []
    assert "always" in capsys.readouterr().err
