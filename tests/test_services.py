"""Domain services over fakes: no FastAPI app, no process-wide databases."""
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app_config import AppConfig, CmsConfig
from demo_data import DemoCatalog, DemoIndex
from services.adapters import CmsGateway, ScriptServerGateway
from services.content import ContentService
from services.errors import ActionFailed, NotFound, Unavailable
from services.products import ProductQuery, ProductService
from services.search import SearchFilters, SearchService
from services.settings import SettingsService

SRC = Path(__file__).parent.parent / "src"


def test_service_modules_do_not_open_global_databases():
    code = ("import sys; import services.container, services.search, services.products, "
            "services.status, services.settings, services.content, api.schemas; "
            "assert 'managers.managers' not in sys.modules, 'managers.managers imported'")
    subprocess.run([sys.executable, "-c", code], cwd=SRC, check=True)


# ── Products and search: the same filters everywhere ──────────────────────────

@pytest.fixture
def demo():
    catalog = DemoCatalog()
    return types.SimpleNamespace(products=ProductService(catalog),
                                 search=SearchService(catalog, DemoIndex()))


def test_is_installed_filters_listing_search_and_query_alike(demo):
    listed = {p["sku"] for p in demo.products.list(ProductQuery(installed=False))["products"]}
    searched = {p["sku"] for p in demo.search.search("street", SearchFilters(installed=False), 50)}
    queried = {h["id"] for h in demo.search.query("street", SearchFilters(installed=False),
                                                  limit=50, offset=0)["results"]}
    assert listed == searched == queried == {"88441"}
    installed = demo.products.list(ProductQuery(installed=True))["products"]
    assert all(p["is_installed"] for p in installed) and len(installed) == 3


def test_filters_are_exact_or_within_and_across(demo):
    f = SearchFilters(categories=("clothes", "props"), compatible_figures=("Genesis 9",))
    assert [p["sku"] for p in demo.search.search("outfit", f, 50)] == ["91123"]
    f = SearchFilters(artists=("Val",))  # no substring matches
    assert demo.search.search("outfit", f, 50) == []


def test_min_relevance_cut_is_shared(demo):
    everything = demo.search.search("cyberpunk outfit", SearchFilters(), 50)
    best = max(p["relevance_score"] for p in everything)
    cut = demo.search.search("cyberpunk outfit", SearchFilters(), 50, min_relevance=best)
    hits = demo.search.query("cyberpunk outfit", SearchFilters(), limit=50, offset=0,
                             min_relevance=best)["results"]
    assert [p["sku"] for p in cut] == [h["id"] for h in hits] == ["91123"]


def test_product_not_found(demo):
    with pytest.raises(NotFound):
        demo.products.get("nope")


def test_failed_migration_is_unavailable():
    catalog = types.SimpleNamespace(migration_error="boom")
    with pytest.raises(Unavailable) as e:
        ProductService(catalog).list(ProductQuery())
    assert e.value.reason == "migration_failed"


# ── Adapters follow the configuration ──────────────────────────────────────────

def test_cms_gateway_reconnects_when_settings_change():
    config = {"cfg": AppConfig(cms=CmsConfig(host="a"))}
    opened = []
    gw = CmsGateway(config=lambda: config["cfg"], factory=lambda cms: opened.append(cms.host) or cms)
    assert gw.get().host == "a" and gw.get().host == "a"
    config["cfg"] = AppConfig(cms=CmsConfig(host="b"))
    assert gw.get().host == "b"
    assert opened == ["a", "b"]


def test_cms_gateway_reports_and_rate_limits_failures():
    calls = []

    def fail(cms):
        calls.append(1)
        raise EnvironmentError("not configured")
    gw = CmsGateway(config=lambda: AppConfig(), factory=fail)
    assert gw.get() is None and gw.get() is None
    assert gw.error == "not configured" and len(calls) == 1


def test_script_server_gateway_follows_url():
    config = {"cfg": AppConfig(daz_script_server_url="http://a")}
    gw = ScriptServerGateway(config=lambda: config["cfg"], factory=lambda url: url)
    assert gw.get() == "http://a"
    config["cfg"] = AppConfig(daz_script_server_url="http://b")
    assert gw.get() == "http://b"


# ── Settings ───────────────────────────────────────────────────────────────────

def test_settings_save_reports_how_changes_apply():
    state = {"cfg": AppConfig()}

    def save(updates):
        cms = state["cfg"].cms.model_copy(update={k[4:]: v for k, v in
                                                 updates.items() if k.startswith("cms_")})
        state["cfg"] = state["cfg"].model_copy(update={
            "cms": cms, **{k: v for k, v in updates.items() if not k.startswith("cms_")}})

    svc = SettingsService(config=lambda: state["cfg"], active_model=lambda: "m/active", save=save)
    out = svc.save({"cms_host": "db.local", "embedding_model": "m/new", "query_model": "x",
                    "cms_password": "", "cms_port": None})
    assert out["changed"] == ["cms_host", "embedding_model"]
    assert out["applied"] == ["cms_host"]
    assert out["applies_on_next_index_job"] == ["embedding_model"]
    assert out["restart_required"] == []
    assert out["ignored"] == ["query_model"]
    assert out["query_model"] == "m/active"
    assert "cms_password" not in out


def test_settings_connection_test_overlays_saved_settings():
    seen = []
    svc = SettingsService(config=lambda: AppConfig(cms=CmsConfig(password="saved")),
                          connect=seen.append)
    assert svc.test_cms({"cms_host": "h", "cms_password": ""})["success"] is True
    assert seen[0].host == "h" and seen[0].password == "saved"
    failing = SettingsService(config=AppConfig, connect=lambda cms: 1 / 0)
    assert failing.test_cms({})["success"] is False


# ── Content actions ────────────────────────────────────────────────────────────

class _Desktop:
    def __init__(self, ok=True):
        self.ok, self.opened = ok, []

    def open_product(self, name):
        self.opened.append(name)
        return self.ok


def _content(desktop, plugin_available=False):
    client = types.SimpleNamespace(is_available=lambda: plugin_available,
                                   browse_to_product=lambda name: {"name": name})
    return ContentService(CmsGateway.fixed(None), ScriptServerGateway.fixed(client), desktop,
                          ProductService(DemoCatalog()))


def test_open_product_prefers_plugin_then_falls_back():
    desktop = _Desktop()
    assert _content(desktop, plugin_available=True).open_product("85227")["via"] == "plugin"
    assert _content(desktop).open_product("85227")["via"] == "subprocess"
    assert desktop.opened == ["Genesis 9 Starter Essentials"]
    with pytest.raises(NotFound):
        _content(desktop).open_product("nope")
    with pytest.raises(ActionFailed):
        _content(_Desktop(ok=False)).open_product("85227")
