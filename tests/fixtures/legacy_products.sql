-- Legacy wide products table exactly as sqlite_db_manager.SQLiteWrapper.setup_sqlite_db creates it.
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, url TEXT, image_url TEXT, store TEXT, name TEXT, artist TEXT, price TEXT, description TEXT,
    tags TEXT, formats TEXT, poly_count TEXT, textures_info TEXT, required_products TEXT, compatible_figures TEXT,
    compatible_software TEXT, embedding_text TEXT, last_updated TEXT, category TEXT, subcategories TEXT, styles TEXT,
    inferred_tags TEXT, enriched_at TEXT, mature INTEGER, asset_count INTEGER
);

INSERT INTO products VALUES (
    'A1', 'https://www.daz3d.com/a1', 'https://img.daz3d.com/a1.jpg', 'DAZ 3D', 'Summer Dress', 'Ann, Bob Studio', '$19.95',
    'A light summer dress.', 'Wardrobe,Clothes, Genesis 8 Female', 'Wardrobe/Clothes', '12,000', '4096 x 4096',
    'Genesis 8 Female Starter', 'Genesis 8 Female', 'DAZ Studio 4.21',
    'A 3D asset package titled ''Summer Dress''. Created by the artist or studio: Ann, Bob Studio. It is categorized under: Wardrobe, Clothes. It contains clothing or wardrobe items for 3D figures. Product Description: A light summer dress.',
    '2024-03-01T10:20:30', 'wardrobe', 'clothes', NULL, NULL, '2024-03-02T08:00:00+00:00', 0, 7
);

INSERT INTO products VALUES (
    'A2', 'https://www.daz3d.com/a2', NULL, 'DAZ 3D', 'Café Props', 'Anna', NULL,
    NULL, 'Props,Decor', 'Props', NULL, NULL,
    NULL, 'Genesis 8.1 Female, Genesis 9', NULL,
    'A 3D asset package titled ''Café Props''. Created by the artist or studio: Anna. It is categorized under: Props, Decor. This is a set of props suitable for decorating digital scenes, environments, and dioramas.',
    '2023-11-05T00:00:00', 'props', 'decor,environments', NULL, NULL, NULL, 1, 3
);

-- Never enriched and never categorised: mostly NULLs.
INSERT INTO products VALUES (
    'A3', NULL, NULL, NULL, 'Bare Product', NULL, NULL,
    NULL, NULL, NULL, NULL, NULL,
    NULL, NULL, NULL,
    'A 3D asset package titled ''Bare Product''.',
    NULL, NULL, '', NULL, NULL, NULL, NULL, NULL
);
