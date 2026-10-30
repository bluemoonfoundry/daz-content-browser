import logging
import sqlite3
from contextlib import contextmanager

from catalog.status import migration_hint
from services.errors import Unavailable

logger = logging.getLogger(__name__)


@contextmanager
def catalog_reads(catalog):
    """Turns an unusable catalog into Unavailable with a reason, instead of a bare 500.

    While the catalog migration has failed, product reads are refused outright (startup is
    not blocked; a restart retries the migration). A SQLite error on a read (locked,
    damaged) is reported the same way.
    """
    if catalog.migration_error:
        raise Unavailable("migration_failed", migration_hint(catalog.migration_error))
    try:
        yield
    except sqlite3.Error as e:
        logger.exception("Catalog read failed.")
        raise Unavailable("catalog_unavailable",
                          f"The local catalog could not be read ({type(e).__name__}: {e}). "
                          f"Retry shortly; if it persists, check the server log.")
