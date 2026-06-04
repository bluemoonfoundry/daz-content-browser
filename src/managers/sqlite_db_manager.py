import os
import sqlite3
import json
import logging
from datetime import datetime, timezone

class SQLiteWrapper:
    """A simple SQLite database manager for storing and retrieving product data."""

    def __init__(self, sqlite_db_path, sqlite_db_table):
        """Initializes the SQLiteWrapper with the database path and table name.
        
        Args:
            sqlite_db_path (str): Path to the SQLite database file. 
            sqlite_db_table (str): Name of the table to use within the SQLite database.
        """
        self._logger = logging.getLogger(__name__)
        self.sqlite_db_path     = sqlite_db_path
        self.sqlite_db_table    = sqlite_db_table

    def setup_sqlite_db(self, force_reset:bool=False):
        """Creates the SQLite database and table using the final schema.
        
        Args:
            force_reset (bool): If True, deletes any existing database file before creating a new one
        """

        if force_reset and os.path.exists(self.sqlite_db_path):
            self._logger.info(f"--force: deleting existing SQLite database at {self.sqlite_db_path!r}")
            os.remove(self.sqlite_db_path)

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(f'''
            CREATE TABLE IF NOT EXISTS {self.sqlite_db_table} (
                sku TEXT PRIMARY KEY, url TEXT, image_url TEXT, store TEXT, name TEXT, artist TEXT, price TEXT, description TEXT,
                tags TEXT, formats TEXT, poly_count TEXT, textures_info TEXT, required_products TEXT, compatible_figures TEXT,
                compatible_software TEXT, embedding_text TEXT, last_updated TEXT, category TEXT, subcategories TEXT, styles TEXT,
                inferred_tags TEXT, enriched_at TEXT, mature INTEGER
            )
        ''')
        conn.commit()
        conn.close()
        self._logger.info(f"SQLite database '{self.sqlite_db_path}' / table '{self.sqlite_db_table}' ready.")

    def get_connection(self):
        """Establishes and returns a connection to the SQLite database."""


        try:
            self.connection = sqlite3.connect(self.sqlite_db_path)
            self.connection.row_factory = sqlite3.Row
        except sqlite3.OperationalError as e:
            self._logger.error(f"Error connecting to SQLite: {e}")
            raise e
        return self.connection

    def get_all_skus_from_sqlite(self):
        """Fetches all existing SKUs from the SQLite database.
        
        Returns:
            list: A list of all SKUs in the SQLite database as strings.
        """
        return self._fetchall_query(f"SELECT sku FROM {self.sqlite_db_table}")
    
    def get_content_by_sku_batch(self, sku_batch):
        """Fetches full product data for a given batch of SKUs from SQLite.
        
        Args:
            sku_batch (list): A list of SKUs to fetch data for.

        Returns:    
            list: A list of dictionaries containing product data for the given SKUs.
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        placeholders = ','.join(['?'] * len(sku_batch))
        
        sqlite_query = f"""
            SELECT sku, url, image_url, embedding_text, name, artist, compatible_figures, tags, category, subcategories
            FROM {self.sqlite_db_table}
            WHERE sku IN ({placeholders})
        """
        
        cursor.execute(sqlite_query, sku_batch)
        rows_to_embed = cursor.fetchall()
        conn.close()

        return rows_to_embed

    def _fetchall_query(self, query) -> list:
        """Helper method to execute a query and return all results as a list of SKUs.

        Args:
            query (str): The SQL query to execute.

        Returns:
            list: A list of SKUs as strings.
        """

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(query)
            # fetchall returns a list of tuples, e.g., [('sku1',), ('sku2',)]
            results = [row[0] for row in cursor.fetchall()]
        except sqlite3.OperationalError as e:
            print(f"Error querying SQLite: {e}. The table might not exist yet.")
            results = []
        finally:
            conn.close()
        return results

    def execute_fetchone_query(self, query: str):
        """Executes a query and returns a single result.

        Args:
            query (str): The SQL query to execute.

        Returns:
            any: The first column of the first row of the result, or None if no result.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            row = cursor.fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError as e:
            self._logger.error(f"Error reading from SQLite: {e}")
            return None
        finally:
            conn.close()

    def execute_fetchall_query(self, query: str) -> list:
        """Executes a query and returns all results.

        Args:
            query (str): The SQL query to execute.

        Returns:
            list: A list of all rows returned by the query.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(query)
            return cursor.fetchall()
        except sqlite3.OperationalError as e:
            self._logger.error(f"Error reading from SQLite: {e}")
            return []
        finally:
            conn.close()
    
    def count(self) -> int:
        """Returns the total number of rows in the SQLite table."""
        q = f"SELECT count(*) from {self.sqlite_db_table}"
        rv = self.execute_fetchone_query(q)
        if rv is None:
            return -1
        else:
            return int(rv)

    def get_sku_row(self, sku) -> dict|None:
        """Fetches the full row for a given SKU from SQLite.

        Args:
            sku (str): The SKU to fetch data for.

        Returns:
            dict|None: A dictionary containing the product data for the given SKU, or None if not found.

        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT * FROM {self.sqlite_db_table} WHERE sku = ?", (sku,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError as e:
            self._logger.error(f"Error querying SQLite: {e}")
            return None
        finally:
            conn.close()
        
    def get_post_checkpoint_rows(self, columns: list[str], checkpoint: str) -> list[dict]:
        """Fetches all rows updated after a given checkpoint timestamp.

        Args:
            columns (list[str]): List of column names to retrieve.
            checkpoint (str): ISO 8601 formatted timestamp string.

        Returns:
            list[dict]: A list of dictionaries representing the rows updated after the checkpoint.
        """
        allowed = {
            "sku", "url", "image_url", "store", "name", "artist", "price", "description",
            "tags", "formats", "poly_count", "textures_info", "required_products",
            "compatible_figures", "compatible_software", "embedding_text", "last_updated",
            "category", "subcategories", "styles", "inferred_tags", "enriched_at", "mature",
        }
        for col in columns:
            if col not in allowed:
                raise ValueError(f"Invalid column name: {col!r}")

        col_clause = ", ".join(columns)
        q = f"""
        SELECT {col_clause}
        FROM {self.sqlite_db_table}
        WHERE Datetime(last_updated) > Datetime(?)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(q, (checkpoint,))
            rows = cursor.fetchall()
            return [dict(zip(row.keys(), row)) for row in rows]
        except sqlite3.OperationalError as e:
            self._logger.error(f"Error querying SQLite: {e}")
            return []
        finally:
            conn.close()
        
    def get_filter_values(self) -> dict:
        """Returns distinct values for every filterable search field.

        Cheaper than loading all ChromaDB metadata — queries SQLite directly.
        Comma-separated fields (compatible_figures, tags, artist) are split and deduplicated.

        Returns:
            dict: Keys 'categories', 'compatible_figures', 'artists', each a sorted list of strings.
        """
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT DISTINCT category FROM {self.sqlite_db_table} "
                "WHERE category IS NOT NULL AND category != ''"
            )
            categories = sorted(r[0] for r in cursor.fetchall())

            def _split_all(sql) -> list:
                cursor.execute(sql)
                result = set()
                for (value,) in cursor.fetchall():
                    for part in value.split(","):
                        if part.strip():
                            result.add(part.strip())
                return sorted(result)

            artists = _split_all(
                f"SELECT artist FROM {self.sqlite_db_table} "
                "WHERE artist IS NOT NULL AND artist != ''"
            )
            figures = _split_all(
                f"SELECT compatible_figures FROM {self.sqlite_db_table} "
                "WHERE compatible_figures IS NOT NULL AND compatible_figures != ''"
            )

            return {"categories": categories, "artists": artists, "compatible_figures": figures}
        except Exception as e:
            self._logger.error(f"Error fetching filter values: {e}")
            return {"categories": [], "artists": [], "compatible_figures": []}
        finally:
            conn.close()

    def get_products(
        self,
        page: int = 1,
        page_size: int = 25,
        category: str | None = None,
        artist: str | None = None,
        compatible_figure: str | None = None,
        name_query: str | None = None,
        sort_by: str = "name",
        sort_dir: str = "asc",
    ) -> dict:
        """Returns a paginated, filtered, sorted list of products.

        Returns:
            dict: Keys 'products' (list of row dicts), 'total', 'page', 'page_size', 'total_pages'.
        """
        valid_sort = {"name", "install_date", "last_updated", "artist"}
        sort_col = sort_by if sort_by in valid_sort else "name"
        if sort_col == "install_date":
            sort_col = "enriched_at"
        order = "ASC" if sort_dir.lower() == "asc" else "DESC"

        conditions: list[str] = []
        params: list = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if artist:
            conditions.append("(artist LIKE ? OR artist LIKE ? OR artist LIKE ? OR artist = ?)")
            params += [f"%,{artist},%", f"{artist},%", f"%,{artist}", artist]
        if compatible_figure:
            cf = compatible_figure
            conditions.append(
                "(compatible_figures LIKE ? OR compatible_figures LIKE ? "
                "OR compatible_figures LIKE ? OR compatible_figures = ?)"
            )
            params += [f"%,{cf},%", f"{cf},%", f"%,{cf}", cf]
        if name_query:
            conditions.append("name LIKE ?")
            params.append(f"%{name_query}%")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM {self.sqlite_db_table} {where}", params
            )
            total = cursor.fetchone()[0]

            offset = (page - 1) * page_size
            cursor.execute(
                f"SELECT * FROM {self.sqlite_db_table} {where} "
                f"ORDER BY {sort_col} {order} LIMIT ? OFFSET ?",
                params + [page_size, offset],
            )
            rows = [dict(r) for r in cursor.fetchall()]
            total_pages = max(1, (total + page_size - 1) // page_size)
            return {
                "products": rows,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }
        except Exception as e:
            self._logger.error(f"Error in get_products: {e}")
            return {"products": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 1}
        finally:
            conn.close()

    def insert_item(self, item):
        """Inserts or updates a product item in the SQLite database.

        Args:
            item (dict): A dictionary containing the product data to insert or update.  
        """ 
        for key, value in item.items():
            if isinstance(value, list):
                item[key] = json.dumps(value)

        # Only set last_updated if the caller didn't provide one
        if not item.get("last_updated"):
            item["last_updated"] = datetime.now(timezone.utc).isoformat()

        # Prepare columns and placeholders for upsert
        columns = ", ".join(item.keys())
        placeholders = ", ".join(["?"] * len(item))
        table = self.sqlite_db_table
        sql = f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})"

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, list(item.values()))
            conn.commit()
            return True
        except Exception as e:
            self._logger.error(f"SQLite exception: {e}")
            return False
        finally:
            conn.close()
        
    def close(self):
        """Closes the SQLite database connection."""
        self.connection.close()

