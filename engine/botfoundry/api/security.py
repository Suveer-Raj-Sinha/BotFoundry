import secrets
from fastapi import Header, HTTPException, Request, status

from ..config import TOKEN_HEADER


def require_token(request: Request, x_botfoundry_token: str | None = Header(default=None)) -> None:
    """Reject any request that does not carry the per-launch token."""
    expected = request.app.state.settings.token
    if not x_botfoundry_token or not secrets.compare_digest(x_botfoundry_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {TOKEN_HEADER} header.",
        )
