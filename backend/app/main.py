import sys
import asyncio

# On Windows, ProactorEventLoop is required for asyncio.create_subprocess_exec used by browser-use
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router
from app.api.v1.endpoints.cdp_bridge import cdp_bridge
from app.core.config import settings
from app.core.logger import logger
from app.core.security import get_active_token

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for startup and shutdown procedures."""
    logger.info("Initializing Web-Frame Agent backend...")
    logger.info(f"LLM Provider: {settings.llm_provider}")
    logger.info(f"LLM Model: {settings.llm_model_name}")
    yield

    logger.info("Shutting down Web-Frame Agent backend...")
    logger.info("Backend shutdown complete.")


app = FastAPI(
    title="Web-Frame Agent API",
    description="Backend API for autonomous in-iframe web navigation and page interaction via Browser-Use and a CDP bridge.",
    version="0.1.0",
    lifespan=lifespan,
)

# Host validation middleware (DNS rebinding protection)
class HostHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0].strip().lower()
        allowed_hosts = set(settings.parsed_allowed_hosts)
        if host and host not in allowed_hosts:
            logger.warning(f"Rejected request with unauthorized Host header: '{host}'")
            return JSONResponse(status_code=400, content={"detail": f"Invalid Host header: {host}"})
        return await call_next(request)

app.add_middleware(HostHeaderMiddleware)

# CORS middleware configuration with strict allowed methods & headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.parsed_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-API-Token", "Accept"],
)

# Mount API routers
app.include_router(api_router, prefix="/api/v1")


@app.get("/", tags=["Health"])
async def root():
    return {
        "name": "Web-Frame Agent API",
        "version": "0.1.0",
        "status": "operational",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "healthy",
        "loop": type(asyncio.get_running_loop()).__name__,
        "browser_running": False,
        "extension_connected": cdp_bridge.is_extension_connected,
        "iframe_ready": cdp_bridge.is_iframe_ready,
        "auth_configured": bool(settings.api_token),
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model_name,
        "vertex_project": settings.google_cloud_project,
        "vertex_location": settings.google_cloud_location,
        "vertex_model": settings.llm_model_name,
        "default_target_url": settings.default_target_url,
        "frontend_url": settings.frontend_url,
        "backend_ws_url": f"ws://{settings.backend_host}:{settings.backend_port}/api/v1/cdp/extension",
    }
