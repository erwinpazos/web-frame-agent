"""Security module providing authentication tokens and Origin validation for REST & WebSockets."""
import secrets
import time
from collections import defaultdict
from typing import Optional, List, Dict
from fastapi import Request, HTTPException, status, WebSocket
from app.core.config import settings
from app.core.logger import logger

# Token configuration is strictly validated by Pydantic Settings at startup.
# Rate limiting in-memory storage: IP -> List[timestamps]
_RATE_LIMIT_STORE = defaultdict(list)
_TASK_LAUNCH_STORE = defaultdict(list)

# Single-use ephemeral bootstrap tokens for local CDP discovery: token -> expire_at_timestamp
_BOOTSTRAP_TOKENS: Dict[str, float] = {}

# Ephemeral derived session tickets: session_token -> expire_at_timestamp (default TTL: 2 hours)
_SESSION_TICKETS: Dict[str, float] = {}
DEFAULT_SESSION_TICKET_TTL = 7200.0  # 2 hours

def check_rate_limit(key: str, max_requests: int, window_seconds: float) -> bool:
    """Sliding window rate limiter. Returns True if allowed, False if exceeded."""
    now = time.time()
    cutoff = now - window_seconds
    timestamps = [t for t in _RATE_LIMIT_STORE[key] if t > cutoff]
    if len(timestamps) >= max_requests:
        return False
    timestamps.append(now)
    _RATE_LIMIT_STORE[key] = timestamps
    return True


def check_task_rate_limit(key: str = "global", max_launches: int = 5, window_seconds: float = 60.0) -> bool:
    """Enforces maximum task launches per minute."""
    now = time.time()
    cutoff = now - window_seconds
    timestamps = [t for t in _TASK_LAUNCH_STORE[key] if t > cutoff]
    if len(timestamps) >= max_launches:
        return False
    timestamps.append(now)
    _TASK_LAUNCH_STORE[key] = timestamps
    return True


def get_active_token() -> str:
    """Returns the configured security token from settings."""
    return settings.api_token.strip()


def mint_cdp_bootstrap_token(ttl_seconds: float = 30.0) -> str:
    """Mints a single-use ephemeral token valid strictly for CDP DevTools WebSocket connections."""
    now = time.time()
    # Clean expired bootstrap tokens
    expired = [t for t, exp in _BOOTSTRAP_TOKENS.items() if exp < now]
    for t in expired:
        _BOOTSTRAP_TOKENS.pop(t, None)

    token = secrets.token_urlsafe(16)
    _BOOTSTRAP_TOKENS[token] = now + ttl_seconds
    return token
def mint_session_ticket(ttl_seconds: float = DEFAULT_SESSION_TICKET_TTL) -> str:
    """Mints an ephemeral derived session ticket with automatic TTL expiration."""
    now = time.time()
    # Prune expired session tickets
    expired = [t for t, exp in _SESSION_TICKETS.items() if exp < now]
    for t in expired:
        _SESSION_TICKETS.pop(t, None)

    ticket = secrets.token_urlsafe(32)
    _SESSION_TICKETS[ticket] = now + ttl_seconds
    return ticket


def verify_session_ticket(token: Optional[str]) -> bool:
    """Verifies if a provided token is a valid active derived session ticket."""
    if not token:
        return False
    now = time.time()
    expire_at = _SESSION_TICKETS.get(token.strip())
    if expire_at is None:
        return False
    if expire_at <= now:
        _SESSION_TICKETS.pop(token.strip(), None)
        return False
    return True


def verify_cdp_bootstrap_token(token: Optional[str]) -> bool:
    """Verifies and consumes a single-use ephemeral bootstrap token."""
    if not token:
        return False
    now = time.time()
    expire_at = _BOOTSTRAP_TOKENS.pop(token.strip(), None)
    if expire_at is None:
        return False
    return expire_at > now


def verify_auth_token(token: Optional[str], allow_cdp_bootstrap: bool = False) -> bool:
    """Verifies a provided token against the master API_TOKEN, derived session tickets, or CDP bootstrap tokens."""
    if not token:
        return False
    clean_token = token.strip()

    # 1. Single-use CDP bootstrap token check (for DevTools WebSocket discovery)
    if allow_cdp_bootstrap and verify_cdp_bootstrap_token(clean_token):
        return True

    # 2. Ephemeral session ticket check (used by frontend UI and Chrome extension)
    if verify_session_ticket(clean_token):
        return True

    # 3. Master API_TOKEN comparison (used by CLI, test suite, and direct admin commands)
    active = get_active_token()
    return secrets.compare_digest(clean_token, active)
def extract_token_from_request(request: Request) -> Optional[str]:
    """Extracts authorization token from query param 'token' or 'Authorization: Bearer <token>' header."""
    token = request.query_params.get("token")
    if token:
        return token

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    elif auth_header:
        return auth_header.strip()

    api_token_header = request.headers.get("X-API-Token")
    if api_token_header:
        return api_token_header.strip()

    return None


def extract_token_from_websocket(websocket: WebSocket) -> Optional[str]:
    """Extracts token from WebSocket query param, Sec-WebSocket-Protocol, or Authorization header."""
    token = websocket.query_params.get("token")
    if token:
        return token

    protocols = websocket.headers.get("sec-websocket-protocol", "")
    if protocols:
        for proto in protocols.split(","):
            p = proto.strip()
            if p.startswith("token."):
                return p.replace("token.", "", 1)
            elif p.startswith("bearer."):
                return p.replace("bearer.", "", 1)

    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    elif auth_header:
        return auth_header.strip()

    return None


def is_allowed_origin(origin: Optional[str]) -> bool:
    """Checks if an Origin header is explicitly in allowed CORS origins or from a chrome-extension."""
    if not origin:
        return True
    origin_clean = origin.strip().lower()
    if origin_clean.startswith("chrome-extension://"):
        ext_id = origin_clean.replace("chrome-extension://", "").split("/")[0].strip()
        # Fail-closed: only the pinned authorized extension ID is allowed, zero fallback!
        if settings.allowed_extension_id:
            return ext_id == settings.allowed_extension_id.strip().lower()
        return False
    allowed = [o.lower().strip() for o in settings.parsed_cors_origins]
    return origin_clean in allowed


async def authenticate_http_request(request: Request) -> None:
    """Dependency for securing REST endpoints. Raises HTTPException 401 if unauthorized."""
    token = extract_token_from_request(request)
    if not verify_auth_token(token):
        logger.warning(f"Unauthorized HTTP request to {request.url.path} from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing security token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def rate_limit_task_launch(request: Request) -> None:
    """Dependency to prevent task launch spamming."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    if not check_task_rate_limit(client_ip, max_launches=5, window_seconds=60.0):
        logger.warning(f"Rate limit exceeded for task launch from {client_ip}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many task launches. Please wait before starting a new mission.",
        )


async def authenticate_websocket(websocket: WebSocket, allow_cdp_bootstrap: bool = False) -> bool:
    """Validates Origin and auth token during WebSocket handshake. Accepts connection if valid, closes with 1008 if rejected."""
    origin = websocket.headers.get("origin")
    if origin and not is_allowed_origin(origin):
        logger.warning(f"WebSocket rejected: unauthorized origin '{origin}'")
        await websocket.accept()
        await websocket.close(code=1008, reason="Unauthorized origin")
        return False

    token = extract_token_from_websocket(websocket)
    if not verify_auth_token(token, allow_cdp_bootstrap=allow_cdp_bootstrap):
        logger.warning(f"WebSocket rejected: invalid or missing token on {websocket.url.path}")
        await websocket.accept()
        await websocket.close(code=1008, reason="Unauthorized token")
        return False

    await websocket.accept()
    return True
