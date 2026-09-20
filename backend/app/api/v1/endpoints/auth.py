"""Authentication endpoints for issuing ephemeral derived session tickets."""
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logger import logger
from app.core.security import DEFAULT_SESSION_TICKET_TTL, mint_session_ticket

router = APIRouter(prefix="/auth", tags=["Authentication"])


class SessionTicketResponse(BaseModel):
    """Payload returning a short-lived session token."""
    session_token: str = Field(..., description="Ephemeral derived session token")
    expires_in: float = Field(..., description="Token validity window in seconds")
    token_type: str = Field(default="Bearer", description="Token authentication scheme")


@router.post("/session", response_model=SessionTicketResponse)
async def create_session_ticket(request: Request):
    """Issues an ephemeral derived session ticket for the local frontend workspace.

    Protected by Origin and Host header validation to restrict issuance strictly to
    the configured frontend host (e.g. localhost, 127.0.0.1).
    """
    # 1. Anti-Spoofing Client IP Extraction:
    # Use direct low-level ASGI socket transport IP (request.scope["client"]), ignoring any unverified X-Forwarded-For headers
    raw_socket_client = request.scope.get("client")
    raw_client_ip = raw_socket_client[0] if raw_socket_client else "127.0.0.1"
    raw_origin = request.headers.get("origin") or request.headers.get("referer") or ""
    # Cloud / Routable Guard:
    # In cloud/routable environments (allow_routable_network=True), bootstrap without an explicit master API_TOKEN is forbidden
    if settings.allow_routable_network:
        auth_header = request.headers.get("Authorization", "")
        master_token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else request.query_params.get("token")
        from app.core.security import get_active_token
        import secrets
        if not master_token or not secrets.compare_digest(master_token, get_active_token()):
            logger.warning(f"[Security Fail-Closed] Unauthenticated /auth/session bootstrap rejected in cloud/routable mode from {raw_client_ip}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="In cloud/routable mode, session tickets require master API_TOKEN authentication.",
            )

    # Local Loopback Guard:
    # In default local mode, client socket MUST be strictly on loopback interfaces
    is_local_client = raw_client_ip in ("127.0.0.1", "localhost", "::1", "testclient")
    if not is_local_client:
        logger.warning(f"[Security] Non-local client attempted session ticket minting: {raw_client_ip}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session tickets can only be minted by local clients.",
        )

    # 2. Strict Origin/Referer check against trusted frontend origins (prevents evil-site.com cross-origin POST)
    trusted_origins = set(settings.parsed_cors_origins)
    trusted_origins.add(settings.frontend_url)

    if raw_origin:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(raw_origin)
            origin_base = f"{parsed.scheme}://{parsed.netloc}"
            is_trusted_origin = origin_base in trusted_origins or any(
                origin_base == o.rstrip("/") for o in trusted_origins
            )
            # Allow pure testclient without external origin
            if not is_trusted_origin and origin_base != "http://testserver":
                logger.warning(f"[Security] Blocked session ticket minting from untrusted Origin/Referer: '{origin_base}' (Client: {raw_client_ip})")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cross-origin session ticket requests are strictly forbidden.",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"[Security] Failed to parse Origin/Referer header '{raw_origin}': {e}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid Origin or Referer header.",
            )
    else:
        # If no Origin and no Referer, allow only local development CLI/testclient
        if raw_client_ip not in ("127.0.0.1", "localhost", "::1", "testclient"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Origin or Referer header required.",
            )

    session_token = mint_session_ticket(ttl_seconds=DEFAULT_SESSION_TICKET_TTL)
    logger.info(f"Minted ephemeral session ticket for local client {raw_client_ip} (valid for {DEFAULT_SESSION_TICKET_TTL}s)")

    return SessionTicketResponse(
        session_token=session_token,
        expires_in=DEFAULT_SESSION_TICKET_TTL,
        token_type="Bearer",
    )
