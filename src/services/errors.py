"""Domain errors. The API maps each to an HTTP status (see api/app.py); services never
raise HTTPException, so they can be used and tested without FastAPI."""


class ServiceError(Exception):
    status_code = 500

    def __init__(self, detail):
        super().__init__(detail if isinstance(detail, str) else repr(detail))
        self.detail = detail


class NotFound(ServiceError):
    status_code = 404


class Forbidden(ServiceError):
    status_code = 403


class ActionFailed(ServiceError):
    status_code = 500


class Unavailable(ServiceError):
    """A dependency can't serve the request; ``reason`` is a stable code, ``repair`` a hint."""
    status_code = 503

    def __init__(self, reason: str, repair: str):
        super().__init__({"reason": reason, "repair": repair})
        self.reason = reason
        self.repair = repair


class ComponentUnavailable(ServiceError):
    """An optional component (e.g. the DAZ Script Server plugin) is not running."""
    status_code = 503
