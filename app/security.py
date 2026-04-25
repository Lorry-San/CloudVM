from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings


api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)


def require_api_token(
    token: str | None = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    if not token or token != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
