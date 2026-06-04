"""Quick audit of non-DAZ-store products in the DAZ CMS PostgreSQL database."""

import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    dbname=os.environ["DB_NAME"],
    user=os.environ["DB_USER"],
    password=os.environ.get("DB_PASS", ""),
    host=os.environ["DB_HOST"],
    port=os.environ["DB_PORT"],
)

with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

    cur.execute("SELECT COUNT(*) FROM dzcontent.product")
    total = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM dzcontent.product
        WHERE token IS NOT NULL AND token != ''
    """)
    with_sku = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM dzcontent.product
        WHERE token IS NULL OR token = ''
    """)
    without_sku = cur.fetchone()[0]

    print(f"Total products:        {total:>6}")
    print(f"  With DAZ SKU:        {with_sku:>6}")
    print(f"  Without DAZ SKU:     {without_sku:>6}")
    print()

    cur.execute("""
        SELECT
            p.name,
            p.token,
            COALESCE(p.last_update, p.date_installed)::text AS last_modified,
            COUNT(DISTINCT c.id) AS file_count,
            STRING_AGG(DISTINCT ct."fldType", ', ') AS content_types
        FROM dzcontent.product AS p
        LEFT JOIN dzcontent.content AS c ON p.id = c.product_id
        LEFT JOIN dzcontent."tblType" AS ct ON c.content_type_id = ct."RecID"
        WHERE p.token IS NULL OR p.token = ''
        GROUP BY p.id, p.name, p.token, p.last_update, p.date_installed
        ORDER BY p.name
        LIMIT 50
    """)
    rows = cur.fetchall()

    if rows:
        print(f"Sample of up to 50 products without a DAZ SKU:")
        print(f"{'Name':<60} {'Files':>5}  {'Content Types'}")
        print("-" * 100)
        for r in rows:
            print(f"{r['name']:<60} {r['file_count']:>5}  {r['content_types'] or '(none)'}")

conn.close()
