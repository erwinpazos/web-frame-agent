import sys
import asyncio
import uvicorn
from app.core.config import settings

# Enforce ProactorEventLoop on Windows so asyncio.create_subprocess_exec works
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def main():
    """Start FastAPI uvicorn server with Windows ProactorEventLoop and reliable Ctrl+C handling."""
    print(f"Starting Web-Frame Agent backend on http://{settings.backend_host}:{settings.backend_port}")
    config = uvicorn.Config(
        "app.main:app",
        host=settings.backend_host,
        port=settings.backend_port,
        reload=False,
        loop="asyncio",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except (KeyboardInterrupt, SystemExit):
        print("\nCtrl+C detected, shutting down backend cleanly...")
        server.should_exit = True
        server.force_exit = True
        sys.exit(0)


if __name__ == "__main__":
    main()
