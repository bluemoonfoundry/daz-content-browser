from managers.etl_transforms import (
    determine_categories,
    determine_compatibility,
    generate_embedding_text,
)


def test_determine_categories_prefers_priority_word():
    rv = determine_categories("People/Clothes/Props")
    assert rv["category"] in {"clothes", "props"}
    assert isinstance(rv["subcategories"], list)


def test_determine_categories_empty_input():
    rv = determine_categories("")
    assert rv == {"category": None, "subcategories": []}


def test_determine_compatibility_from_compat_string():
    figures = ["Genesis 8 Female", "Genesis 9"]
    product = {
        "product_compatibility": "Works with Genesis 8 Female and others",
        "product_name": "Some Asset",
        "description": "",
    }
    rv = determine_compatibility(product, figures)
    assert rv["new_compatibility"] == "Genesis 8 Female"
    assert rv["tags_to_append"] == "Works with Genesis 8 Female and others"


def test_determine_compatibility_fallback_to_name_and_description():
    figures = ["Genesis 8 Female", "Genesis 9"]
    product = {
        "product_compatibility": "",
        "product_name": "Ultra Hair for Genesis 9",
        "description": "",
    }
    rv = determine_compatibility(product, figures)
    assert rv["new_compatibility"] == "Genesis 9"


def test_generate_embedding_text_contains_key_fields():
    product = {
        "product_name": "Cyber Outfit",
        "artists": "Artist A",
        "categories": "Props, Clothes",
    }
    web = {"description": "A gritty futuristic wardrobe set."}
    text = generate_embedding_text(product, web)
    assert "Cyber Outfit" in text
    assert "Artist A" in text
    assert "Product Description:" in text
