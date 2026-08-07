import gzip
import json
import os

from dsf_parser import parse_dsf_file, extract_referenced_guids, _resolve_target_figure

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dsf")


def test_parses_plain_morph():
    result = parse_dsf_file(os.path.join(FIXTURES, "Billow.dsf"))
    assert result is not None
    assert result.name == "Billow"
    assert result.label == "Billow"
    assert result.group_path == "Actor/CloakStyled"
    assert result.vertex_count == 23369
    assert len(result.deltas) == 18503
    assert result.deltas[0][0] == 0
    assert result.min_value == 0.0
    assert result.max_value == 1.0
    assert result.is_clamped is True
    assert result.formulas_json is None
    assert result.guid  # asset_info.id, non-empty


def test_parses_gzip_compressed_morph(tmp_path):
    # Real DAZ .dsf files are frequently gzip-compressed despite the plain
    # ".dsf" extension (no ".gz" suffix) -- detected via magic bytes, not
    # naming. Confirmed against ~37% of a random sample of the user's real
    # library. Build a compressed copy of the real Billow.dsf fixture.
    gz_path = tmp_path / "Billow.dsf"
    with open(os.path.join(FIXTURES, "Billow.dsf"), "rb") as src:
        raw = src.read()
    with gzip.open(gz_path, "wb") as dst:
        dst.write(raw)

    result = parse_dsf_file(str(gz_path))
    assert result is not None
    assert result.name == "Billow"
    assert result.vertex_count == 23369
    assert len(result.deltas) == 18503


def test_parses_jcm_with_bone_driven_formula():
    result = parse_dsf_file(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"))
    assert result is not None
    assert result.formulas_json is not None
    formulas = json.loads(result.formulas_json)
    assert formulas[0]["operations"][0]["op"] == "push"
    assert "rotation/x" in formulas[0]["operations"][0]["url"]


def test_returns_none_for_non_modifier_json(tmp_path):
    path = tmp_path / "not_a_modifier.dsf"
    path.write_text(json.dumps({"asset_info": {"type": "geometry", "id": "/x/y.dsf"}}))
    assert parse_dsf_file(str(path)) is None


def test_returns_none_for_modifier_with_no_deltas(tmp_path):
    path = tmp_path / "no_deltas.dsf"
    path.write_text(json.dumps({
        "asset_info": {"type": "modifier", "id": "/x/y.dsf"},
        "modifier_library": [{"id": "y", "name": "y", "channel": {}}],
    }))
    assert parse_dsf_file(str(path)) is None


def test_extract_referenced_guids_from_bone_driven_formula():
    result = parse_dsf_file(os.path.join(FIXTURES, "pJCMCloakBend_m90.dsf"))
    refs = extract_referenced_guids(result.formulas_json)
    # The real fixture references a bone rotation on the cloak geometry, not
    # another morph -- it should still be extracted as a raw path (dependency
    # resolution against known morph guids happens later, in the SQLite layer).
    assert len(refs) == 1
    assert refs[0].endswith("GnHdCloak_G3_23369.dsf")


def test_extract_referenced_guids_handles_multiple_operations_and_formulas():
    formulas_json = json.dumps([
        {
            "output": "Fig:#morphA?value",
            "operations": [
                {"op": "push", "url": "Fig:/data/lib/MorphB.dsf#MorphB?value"},
                {"op": "push", "val": 0.5},
                {"op": "mult"},
            ],
        },
        {
            "output": "Fig:#morphA?value",
            "operations": [
                {"op": "push", "url": "Fig:/data/lib/MorphC.dsf#MorphC?value"},
            ],
        },
    ])
    refs = extract_referenced_guids(formulas_json)
    assert refs == ["/data/lib/MorphB.dsf", "/data/lib/MorphC.dsf"]


def test_extract_referenced_guids_returns_empty_list_for_none():
    assert extract_referenced_guids(None) == []


def test_extract_referenced_guids_handles_pathless_name_form():
    # The dominant real-world DAZ formula operand shape: no /data/... path,
    # just a figure label and a property/channel name after "#".
    formulas_json = json.dumps([
        {
            "output": "Fig:#pJCMSomething?value",
            "operations": [
                {"op": "push", "url": "Fig:#pJCMSomething?value"},
            ],
        },
    ])
    refs = extract_referenced_guids(formulas_json)
    assert refs == ["name:pJCMSomething"]


def test_resolve_target_figure_from_url_encoded_parent():
    """A realistic URL-encoded parent URL should resolve to the figure name."""
    parent = "/data/%21Daz%20Original/G3HoodedCloak/Hooded%20Cloak/GnHdCloak_G3_23369.dsf#geometry"
    result = _resolve_target_figure(parent)
    assert result == "GnHdCloak_G3_23369"


def test_resolve_target_figure_returns_none_for_none_input():
    """None input should return None."""
    result = _resolve_target_figure(None)
    assert result is None


def test_resolve_target_figure_returns_none_for_empty_string():
    """Empty string input should return None."""
    result = _resolve_target_figure("")
    assert result is None
