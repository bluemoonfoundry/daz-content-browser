import pytest
from managers.morph_index_manager import MorphIndexManager


def make_record(guid="guid-1", **overrides):
    record = {
        "guid": guid,
        "label": "Billow",
        "name": "Billow",
        "target_figure": "GnHdCloak_G3_23369",
        "group_path": "Actor/CloakStyled",
        "source_dsf_path": r"X:\lib\data\Billow.dsf",
        "tmb_path": "data/Billow.tmb",
        "vertex_count": 23369,
        "delta_count": 18503,
        "min_value": 0.0,
        "max_value": 1.0,
        "is_clamped": True,
        "formulas_json": None,
        "content_hash": "hash-1",
    }
    record.update(overrides)
    return record


@pytest.fixture
def manager(tmp_path):
    mgr = MorphIndexManager(str(tmp_path / "morph_index.db"))
    mgr.setup_db()
    return mgr


def test_insert_and_lookup_by_guid(manager):
    morph_id = manager.insert_morph(make_record())
    assert manager.get_morph_id_by_guid("guid-1") == morph_id


def test_get_content_hash_returns_none_for_unknown_guid(manager):
    assert manager.get_content_hash("nope") is None


def test_get_content_hash_returns_stored_value(manager):
    manager.insert_morph(make_record(content_hash="abc123"))
    assert manager.get_content_hash("guid-1") == "abc123"


def test_insert_morph_upsert_preserves_morph_id_on_same_guid(manager):
    first_id = manager.insert_morph(make_record(label="Billow"))
    second_id = manager.insert_morph(make_record(label="Billow v2", content_hash="new-hash"))
    assert first_id == second_id
    assert manager.get_content_hash("guid-1") == "new-hash"


def test_get_morphs_by_guids(manager):
    manager.insert_morph(make_record(guid="a"))
    manager.insert_morph(make_record(guid="b"))
    manager.insert_morph(make_record(guid="c"))
    rows = manager.get_morphs_by_guids(["a", "c"])
    assert sorted(r["guid"] for r in rows) == ["a", "c"]


def test_get_all_guids(manager):
    manager.insert_morph(make_record(guid="a"))
    manager.insert_morph(make_record(guid="b"))
    manager.insert_morph(make_record(guid="c"))
    assert sorted(manager.get_all_guids()) == ["a", "b", "c"]


def test_get_all_guids_returns_empty_list_for_empty_table(manager):
    assert manager.get_all_guids() == []


def test_rebuild_dependencies(manager):
    manager.insert_morph(make_record(guid="parent-guid", formulas_json='[{"op": "noop"}]'))
    manager.insert_morph(make_record(guid="child-guid"))

    def fake_extract(formulas_json):
        if formulas_json == '[{"op": "noop"}]':
            return ["child-guid", "unresolvable-guid"]
        return []

    count = manager.rebuild_dependencies(fake_extract)
    assert count == 1  # only "child-guid" resolves to a real morph
    stats = manager.get_stats()
    assert stats["morph_count"] == 2
    assert stats["dependency_count"] == 1


def test_rebuild_dependencies_resolves_name_marker(manager):
    # A formula referencing another morph via the pathless "name:" marker
    # form should resolve to a real edge when a morph with that `name`
    # exists in the table.
    manager.insert_morph(make_record(
        guid="parent-guid", name="pJCMParent",
        formulas_json='[{"op": "noop"}]',
    ))
    manager.insert_morph(make_record(guid="child-guid", name="pJCMChild"))

    def fake_extract(formulas_json):
        if formulas_json == '[{"op": "noop"}]':
            return ["name:pJCMChild"]
        return []

    count = manager.rebuild_dependencies(fake_extract)
    assert count == 1
    assert manager.get_stats()["dependency_count"] == 1


def test_get_content_hash_by_source_path_returns_none_for_unknown_path(manager):
    assert manager.get_content_hash_by_source_path(r"X:\lib\data\nope.dsf") is None


def test_rebuild_dependencies_scopes_name_marker_to_dependent_target_figure(manager):
    # Two different figures each have a same-named morph (e.g. pJCMThighBend_35
    # exists on both Genesis 8 and Genesis 9). A "name:" marker on a formula
    # belonging to a Genesis8Female morph must resolve to the Genesis8Female
    # sibling, not the same-named Genesis9Female one.
    manager.insert_morph(make_record(
        guid="g8-parent", name="pJCMParent", target_figure="Genesis8Female",
        formulas_json='[{"op": "noop"}]',
    ))
    manager.insert_morph(make_record(
        guid="g8-child", name="pJCMShared", target_figure="Genesis8Female",
    ))
    manager.insert_morph(make_record(
        guid="g9-child", name="pJCMShared", target_figure="Genesis9Female",
    ))

    def fake_extract(formulas_json):
        if formulas_json == '[{"op": "noop"}]':
            return ["name:pJCMShared"]
        return []

    count = manager.rebuild_dependencies(fake_extract)
    assert count == 1

    conn = manager.get_connection()
    row = conn.execute(
        "SELECT referenced_morph_id FROM morph_dependencies"
    ).fetchone()
    resolved_guid = conn.execute(
        "SELECT guid FROM morphs WHERE morph_id = ?", (row["referenced_morph_id"],)
    ).fetchone()["guid"]
    conn.close()
    assert resolved_guid == "g8-child"


def test_get_content_hash_by_source_path_returns_stored_value(manager):
    manager.insert_morph(make_record(source_dsf_path=r"X:\lib\data\Billow.dsf", content_hash="abc123"))
    assert manager.get_content_hash_by_source_path(r"X:\lib\data\Billow.dsf") == "abc123"


def test_setup_db_force_reset_wipes_existing_data(tmp_path):
    db_path = str(tmp_path / "morph_index.db")
    mgr = MorphIndexManager(db_path)
    mgr.setup_db()
    mgr.insert_morph(make_record())
    assert mgr.get_stats()["morph_count"] == 1

    mgr2 = MorphIndexManager(db_path)
    mgr2.setup_db(force_reset=True)
    assert mgr2.get_stats()["morph_count"] == 0
