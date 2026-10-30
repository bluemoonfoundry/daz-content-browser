from catalog import documents
from catalog.documents import (build_view_row, doc_hash, determine_compatibility,
                                   load_figure_names, payload_hash)

CMS = {"sku": "12345", "product_name": "Desert Robe for Genesis 8", "artists": "Ann, Bo",
       "product_compatibility": "Genesis 8 Female", "content_item_count": 7,
       "categories": "Wardrobe, Outfits", "content_types": "Follower/Wardrobe/Dress",
       "last_modified_date": "2024-01-02T03:04:05"}
WEB = {"url": "https://www.daz3d.com/desert-robe", "description": "A flowing robe.", "price": "$9.99"}
FIGS = ["Genesis 8", "Genesis 8 Female", "Genesis 8.1 Female"]


def test_view_row_matches_legacy_etl_derivation():
    r = build_view_row("daz:12345", CMS, WEB, FIGS)
    assert r["tags"] == "Wardrobe, Outfits, Genesis 8 Female"
    assert r["compatible_figures"] == "Genesis 8, Genesis 8 Female"
    assert r["category"] == "wardrobe" and r["asset_count"] == 7
    assert r["embedding_text"].startswith("A 3D asset package titled 'Desert Robe for Genesis 8'.")
    assert r["embedding_text"].endswith("Product Description: A flowing robe.")
    assert r["artists_list"] == ["Ann", "Bo"]
    assert r["tags_list"] == ["Wardrobe", "Outfits", "Genesis 8 Female"]
    assert r["figures_list"] == ["Genesis 8", "Genesis 8 Female"]
    assert r["last_updated"] == "2024-01-02T03:04:05"
    assert r["formats"] == "Follower/Wardrobe/Dress" and r["subcategories"] == "dress"
    assert r["styles"] is None and r["inferred_tags"] is None
    assert "doc_hash" not in r


def test_figures_ignore_web_description():
    web = dict(WEB, description="Also fits Genesis 8.1 Female")
    assert "Genesis 8.1 Female" not in build_view_row("daz:1", dict(CMS, product_compatibility=None,
                                                       product_name="Robe"), web, FIGS)["compatible_figures"]


def test_determine_compatibility_uses_description_only_when_given():
    p = {"product_name": "Robe", "description": "fits Genesis 8"}
    assert determine_compatibility(p, FIGS)["new_compatibility"] == "Genesis 8"


def test_legacy_payload_uses_legacy_derived_fields():
    legacy = {"tags": "old tag", "compatible_figures": "Genesis 3 Male",
              "category": "props", "subcategories": "a,b"}
    r = build_view_row("daz:1", dict(CMS, legacy=legacy), WEB, FIGS)
    assert (r["tags"], r["compatible_figures"], r["category"], r["subcategories"]) == \
        ("old tag", "Genesis 3 Male", "props", "a,b")
    assert r["figures_list"] == ["Genesis 3 Male"]
    assert r["name"] == CMS["product_name"] and r["asset_count"] == 7


def test_legacy_embedding_text_used_verbatim():
    text = "A 3D asset package titled 'Old'. It is categorized under: Props, Decor. "
    legacy = {"tags": "Props,Decor", "compatible_figures": "", "category": "props",
              "subcategories": "", "embedding_text": text}
    cms = {k: v for k, v in CMS.items() if k != "categories"}  # legacy payloads lack it
    r = build_view_row("daz:1", dict(cms, legacy=legacy), WEB, FIGS)
    assert r["embedding_text"] == text
    # Without the legacy dict the text is derived as usual.
    assert build_view_row("daz:1", cms, WEB, FIGS)["embedding_text"] != text


def test_view_row_without_web_record():
    r = build_view_row("daz:12345", CMS, None, FIGS)
    assert r["url"] is None
    assert "Product Description" not in r["embedding_text"]


def test_none_description_equals_empty_description():
    a = build_view_row("daz:1", CMS, dict(WEB, description=None), FIGS)
    b = build_view_row("daz:1", CMS, dict(WEB, description=""), FIGS)
    assert a["embedding_text"] == b["embedding_text"] and a["description"] is None


def test_list_split_dedupes_and_drops_empties():
    r = build_view_row("daz:1", dict(CMS, artists=" Ann, ,Bo,Ann,"), WEB, FIGS)
    assert r["artists_list"] == ["Ann", "Bo"]


def test_doc_hash_changes_with_text_and_metadata_but_not_price():
    a = build_view_row("daz:1", CMS, WEB, FIGS)
    assert doc_hash(a) == doc_hash(dict(a, price="$1"))
    assert doc_hash(a) != doc_hash(dict(a, embedding_text=a["embedding_text"] + "x"))
    assert doc_hash(a) != doc_hash(dict(a, name="Other"))


def test_doc_hash_depends_on_format_version(monkeypatch):
    a = build_view_row("daz:1", CMS, WEB, FIGS)
    h = doc_hash(a)
    monkeypatch.setattr(documents, "DOC_FORMAT_VERSION", documents.DOC_FORMAT_VERSION + 1)
    assert doc_hash(a) != h


def test_payload_hash_is_key_order_independent():
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_load_figure_names_returns_list_or_raises_with_path():
    try:
        assert isinstance(load_figure_names(), list)
    except FileNotFoundError as e:
        assert ".figures.json" in str(e)
