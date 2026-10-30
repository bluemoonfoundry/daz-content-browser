"""ASGI entry point (``uvicorn server:app``). The application lives in the ``api`` package
(routers, schemas, ``create_app``) over the domain services in ``services``."""
from api.app import create_app

app = create_app()
