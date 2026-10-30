import sqlite3
import threading

import pytest

from catalog import schema
from catalog.schema import LATEST_VERSION, MigrationFailed, connect, migrate


def test_migrate_fresh_db_reaches_latest(tmp_path):
    db = str(tmp_path / "c.db")
    assert migrate(db) == LATEST_VERSION
    names = {r[0] for r in connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"assets", "source_records", "asset_view", "index_generations", "asset_vectors",
            "artists", "asset_artists", "tags", "asset_tags", "figures", "asset_figures"} <= names


def test_migrate_is_idempotent(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(db)
    assert migrate(db) == LATEST_VERSION


def test_coexists_with_legacy_products_table(tmp_path):
    db = str(tmp_path / "c.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT)")
    c.execute("INSERT INTO products VALUES ('1', 'x')")
    c.commit()
    c.close()
    migrate(db)
    assert connect(db).execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def _gen(conn, status, name):
    conn.execute(
        "INSERT INTO index_generations (status, model_id, model_dim, doc_format_version,"
        " schema_version, collection_name, created_at) VALUES (?, 'm', 3, 1, 1, ?, 't')",
        (status, name),
    )


def test_only_one_active_and_one_building_generation(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(db)
    conn = connect(db)
    _gen(conn, "active", "a")  # first build promotes with no prior active row
    _gen(conn, "building", "b")
    _gen(conn, "retired", "r1")
    _gen(conn, "retired", "r2")
    with pytest.raises(sqlite3.IntegrityError):
        _gen(conn, "active", "a2")
    with pytest.raises(sqlite3.IntegrityError):
        _gen(conn, "building", "b2")


def test_connect_pragmas(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(db)
    conn = connect(db)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_concurrent_migrate_runs_once(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    calls = []
    orig = schema._MIGRATIONS[0][1]

    def counted(conn, ctx):
        calls.append(1)
        orig(conn, ctx)

    monkeypatch.setattr(schema, "_MIGRATIONS", [(1, counted)] + schema._MIGRATIONS[1:])
    results, errors = [], []

    def run():
        try:
            results.append(migrate(db))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert results == [LATEST_VERSION] * 4
    assert len(calls) == 1


def test_failed_migration_rolls_back_and_reraises(tmp_path, monkeypatch):
    db = str(tmp_path / "c.db")
    migrate(db)

    def bad(conn, ctx):
        conn.execute("CREATE TABLE half_done (x)")
        raise MigrationFailed(LATEST_VERSION + 1, "nope")

    monkeypatch.setattr(schema, "_MIGRATIONS", schema._MIGRATIONS + [(LATEST_VERSION + 1, bad)])
    monkeypatch.setattr(schema, "LATEST_VERSION", LATEST_VERSION + 1)
    with pytest.raises(MigrationFailed) as ei:
        migrate(db)
    assert ei.value.version == LATEST_VERSION + 1 and ei.value.reason == "nope"
    conn = connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone()


def test_legacy_table_passed_to_migrations(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(schema, "_MIGRATIONS", [(1, lambda c, ctx: seen.append(ctx.legacy_table))])
    monkeypatch.setattr(schema, "LATEST_VERSION", 1)
    migrate(str(tmp_path / "c.db"), legacy_table="products")
    assert seen == ["products"]
