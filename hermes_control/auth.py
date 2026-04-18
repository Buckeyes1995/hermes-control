"""Bearer-token auth dependency."""
import secrets

from fastapi import Header, HTTPException, status

from hermes_control.config import config

_TOKEN: str | None = config.load_token()


async def require_bearer(authorization: str = Header(default="")) -> None:
    if not _TOKEN:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="hermes-control token not configured on the server",
        )
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    provided = authorization[len(prefix):].strip()
    if not secrets.compare_digest(provided, _TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")
