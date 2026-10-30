"""Wires adapters into the domain services the API depends on.

``Services`` is built once per app (``api.app.create_app``) and reached by routes through
FastAPI dependencies, so tests build one from fakes instead of patching module globals.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from app_config import AppConfig, get_config
from services.content import ContentService
from services.jobs import JobService
from services.products import ProductService
from services.search import SearchService
from services.settings import SettingsService
from services.status import StatusService


@dataclass
class Services:
    mode: str
    products: ProductService
    search: SearchService
    status: StatusService
    jobs: JobService
    settings: SettingsService
    content: ContentService
    # Startup work for the lifespan (production only): None in demo mode.
    catalog: object = None
    active_index: object = None
    load_embedding_model: Optional[Callable] = None


def build_services(*, catalog, generations, active_index, job_store, cms, script_server,
                   desktop, spawn_worker: Callable, worker_log: Callable,
                   load_embedding_model: Optional[Callable] = None,
                   config: Callable[[], AppConfig] = get_config,
                   settings: Optional[SettingsService] = None) -> Services:
    """Production services over the given adapters."""
    products = ProductService(catalog)
    jobs = JobService(job_store, spawn_worker)

    def active_model():
        if catalog.migration_error:
            return None
        try:
            gen = generations.active()
        except Exception:
            return None
        return gen["model_id"] if gen else None

    return Services(
        mode="production",
        products=products,
        search=SearchService(catalog, active_index),
        status=StatusService(catalog, generations, active_index, job_store, cms, jobs,
                             worker_log=worker_log),
        jobs=jobs,
        settings=settings or SettingsService(config=config, active_model=active_model),
        content=ContentService(cms, script_server, desktop, products),
        catalog=catalog,
        active_index=active_index,
        load_embedding_model=load_embedding_model,
    )


def build_demo_services(job_store, settings: Optional[SettingsService] = None) -> Services:
    """Demo mode: the in-memory demo catalog and index; no CMS, plugin, or index worker."""
    from demo_data import DemoCatalog, DemoIndex, DemoStatus
    from services.adapters import CmsGateway, DesktopActions, ScriptServerGateway

    catalog = DemoCatalog()
    products = ProductService(catalog)
    jobs = JobService(job_store, spawn_worker=lambda store: None, enabled=False)
    cms = CmsGateway.fixed(None)
    return Services(
        mode="demo",
        products=products,
        search=SearchService(catalog, DemoIndex()),
        status=DemoStatus(catalog),
        jobs=jobs,
        settings=settings or SettingsService(),
        content=ContentService(cms, ScriptServerGateway.fixed(None), DesktopActions(), products,
                               demo=True),
    )


def default_services() -> Services:
    """Services over the process-wide adapters in managers.managers (opened lazily)."""
    from index_worker import spawn_worker, worker_log_path
    from managers import managers as m

    if get_config().app_mode == "demo":
        return build_demo_services(m.job_store)
    import embedding_utils
    return build_services(
        catalog=m.catalog, generations=m.generations, active_index=m.active_index,
        job_store=m.job_store, cms=m.cms, script_server=m.script_server, desktop=m.desktop,
        spawn_worker=spawn_worker, worker_log=worker_log_path,
        load_embedding_model=lambda: embedding_utils.load_embedding_model())
