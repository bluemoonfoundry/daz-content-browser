"""Parses DAZ .dsf modifier files into ParsedMorph records.

Only entries with asset_info.type == "modifier" and a non-empty
morph.deltas.values block are ingestible morphs (see design doc section 6);
everything else returns None.
"""

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote


@dataclass
class ParsedMorph:
    guid: str
    label: str
    name: str
    target_figure: Optional[str]
    group_path: Optional[str]
    vertex_count: int
    deltas: list
    min_value: float
    max_value: float
    is_clamped: bool
    formulas_json: Optional[str]


def _resolve_target_figure(parent_url: Optional[str]) -> Optional[str]:
    """Best-effort figure name from a modifier's `parent` geometry URL, e.g.
    ".../GnHdCloak_G3_23369.dsf#geometry" -> "GnHdCloak_G3_23369".
    Returns None if parent_url is missing or has no usable path segment.
    """
    if not parent_url:
        return None
    path = parent_url.split("#", 1)[0]
    stem = os.path.splitext(os.path.basename(path))[0]
    return unquote(stem) or None


def parse_dsf_file(path: str) -> Optional[ParsedMorph]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)

    asset_info = doc.get("asset_info", {})
    if asset_info.get("type") != "modifier":
        return None
    guid = asset_info.get("id")
    if not guid:
        return None

    for modifier in doc.get("modifier_library", []):
        morph = modifier.get("morph")
        if not morph or not morph.get("deltas", {}).get("values"):
            continue

        channel = modifier.get("channel", {})
        raw_deltas = morph["deltas"]["values"]
        deltas = [(int(v[0]), float(v[1]), float(v[2]), float(v[3])) for v in raw_deltas]

        formulas = modifier.get("formulas")
        formulas_json = json.dumps(formulas) if formulas else None

        return ParsedMorph(
            guid=guid,
            label=channel.get("label") or modifier.get("name") or modifier.get("id"),
            name=modifier.get("name") or modifier.get("id"),
            target_figure=_resolve_target_figure(modifier.get("parent")),
            group_path=modifier.get("group"),
            vertex_count=morph.get("vertex_count", 0),
            deltas=deltas,
            min_value=channel.get("min", 0.0),
            max_value=channel.get("max", 1.0),
            is_clamped=bool(channel.get("clamped", True)),
            formulas_json=formulas_json,
        )

    return None
