import logging
import os
from .daz_db_analyzer import DazDBAnalyzer
from .sqlite_db_manager import SQLiteWrapper
from .chroma_db_manager import ChromaDbManager
from .daz_script_server import DazScriptServerClient

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

sqlite_db = SQLiteWrapper(
    sqlite_db_path  = os.environ.get("SQLITE_DB_PATH"),
    sqlite_db_table = os.environ.get("SQLITE_TABLE_NAME")
)

chroma_db_manager = ChromaDbManager(
    os.environ.get("CHROMA_PATH", "chroma_db"),
    os.environ.get("CHROMA_COLLECTION", "daz_products2")
)

try:
    daz_pg_analyzer = DazDBAnalyzer()
except EnvironmentError as e:
    logger.warning(f"PostgreSQL analyzer not available: {e}")
    daz_pg_analyzer = None

daz_script_server = DazScriptServerClient(
    base_url=os.getenv("DAZ_SCRIPT_SERVER_URL", "http://127.0.0.1:18811")
)