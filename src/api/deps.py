from typing import Annotated

from fastapi import Depends, Request

from security import require_read_access, require_token
from services.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]

# Route dependencies: writes and privileged actions need the token; reads are open to
# loopback clients unless VAB_REQUIRE_TOKEN_FOR_READS is set (see security.py).
AUTH = [Depends(require_token)]
READ = [Depends(require_read_access)]
