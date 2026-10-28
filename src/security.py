"""Local-first API security: per-install token, Host/Origin guards, content-root scoping.

Threat model: the server is a single-user desktop backend bound to loopback. The things
it defends against are (1) web pages in the user's browser reaching http://127.0.0.1
(CSRF, DNS rebinding, <img>/form GETs), and (2) accidental LAN exposure. Local processes
running as the same user can read the token file and are treated as trusted.

Token resolution order:
  1. VAB_API_TOKEN env var (fixed token — useful for tests and scripted clients)
  2. VAB_API_TOKEN_FILE env var, else ~/.daz3d/vab_api_token.txt — generated on first
     run, mirroring the DazScriptServer plugin's ~/.daz3d/dazscriptserver_token.txt.

Clients present the token as `Authorization: Bearer <token>`; the browser UI exchanges a
same-origin POST /api/v1/auth/session for an HttpOnly cookie so <img> thumbnail requests
authenticate too.
"""
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

SESSION_COOKIE = "vab_session"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_DEFAULT_UI_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


# ── Token ──────────────────────────────────────────────────────────────────────

def token_file_path() -> Path:
    override = os.getenv("VAB_API_TOKEN_FILE")
    return Path(override) if override else Path.home() / ".daz3d" / "vab_api_token.txt"


def _load_or_create_token() -> str:
    env_token = os.getenv("VAB_API_TOKEN", "").strip()
    if env_token:
        return env_token
    path = token_file_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info(f"Generated API token at {path}")
    except OSError as e:
        # Still usable for this process via the session cookie; clients just can't read it.
        logger.warning(f"Could not persist API token to {path}: {e}")
    return token


_api_token: Optional[str] = None


def get_api_token() -> str:
    global _api_token
    if _api_token is None:
        _api_token = _load_or_create_token()
    return _api_token


def _presented_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


def is_authenticated(request: Request) -> bool:
    presented = _presented_token(request)
    return bool(presented) and hmac.compare_digest(presented, get_api_token())


def require_token(request: Request) -> None:
    """Dependency for mutating / privileged endpoints."""
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")


def require_read_access(request: Request) -> None:
    """Dependency for read-only catalog endpoints.

    Documented local-only exception: these stay token-free by default so the DAZ Studio
    pane and MCP server keep working without token plumbing. They are still covered by the
    Host/Origin guard, so browsers on other sites can neither read them (CORS) nor reach
    them through DNS rebinding. Set VAB_REQUIRE_TOKEN_FOR_READS=1 to lock them down.
    """
    if _env_flag("VAB_REQUIRE_TOKEN_FOR_READS"):
        require_token(request)


# ── Host / Origin ──────────────────────────────────────────────────────────────

def allowed_hosts() -> set[str]:
    return _LOOPBACK_HOSTS | {h.lower() for h in _env_list("VAB_ALLOWED_HOSTS")}


def allowed_origins() -> list[str]:
    return _env_list("VAB_ALLOWED_ORIGINS") or list(_DEFAULT_UI_ORIGINS)


def _host_only(netloc: str) -> str:
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def is_loopback_bind(host: str) -> bool:
    return host.lower() in _LOOPBACK_HOSTS


def origin_allowed(origin: str, host_header: str) -> bool:
    if origin.rstrip("/") in {o.rstrip("/") for o in allowed_origins()}:
        return True
    # Same-origin: UI served by this server (Origin http://127.0.0.1:8000, Host 127.0.0.1:8000)
    return urlsplit(origin).netloc.lower() == host_header.lower()


async def host_origin_guard(request: Request, call_next):
    """Rejects requests with a non-loopback Host (DNS rebinding / LAN exposure) and API
    requests from foreign browser origins (CSRF, including same-site localhost pages)."""
    host_header = request.headers.get("host", "")
    if _host_only(host_header) not in allowed_hosts():
        return JSONResponse({"detail": "Host not allowed."}, status_code=403)
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, host_header):
            return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
    return await call_next(request)


# ── Content-root scoping ───────────────────────────────────────────────────────

def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def resolve_within_roots(path: str, roots: Iterable[str]) -> Optional[Path]:
    """Returns the resolved path if it lies beneath one of `roots`, else None.

    Resolves `..` segments and symlinks on both sides, so traversal and links that
    escape a root are rejected.
    """
    if not path or not Path(path).is_absolute():
        return None
    try:
        target = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in roots:
        if not root:
            continue
        try:
            root_resolved = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            if os.path.commonpath([_norm(root_resolved), _norm(target)]) == _norm(root_resolved):
                return target
        except ValueError:
            # Different drives on Windows
            continue
    return None
