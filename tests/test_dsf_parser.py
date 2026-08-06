import json
import os

import pytest
from dsf_parser import parse_dsf_file

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
