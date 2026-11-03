"""
Mila Observatory Server — FastAPI application.

Usage:
    python -m observatory.server
    
Runs as a separate process from Mila bot.
Reads/writes shared SQLite database and runtime config.
"""

import sys
import os
import uvicorn
from contextlib import asynccontextmanager

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from Mila_observatory.auth import is_auth_enabled, require_auth, resolve_host
from Mila_observatory.runtime import RUNTIME_CONFIG_PATH
from config import settings
from core.db import init_db
from Mila_observatory.api.dashboard import router as dashboard_router
from Mila_observatory.api.model import router as model_router
from Mila_observatory.api.memory import router as memory_router
from Mila_observatory.api.tools import router as tools_router
from Mila_observatory.api.personality import router as personality_router
from Mila_observatory.api.pipeline import router as pipeline_router
from Mila_observatory.api.config import router as config_router
from Mila_observatory.api.sleep import router as sleep_router
from Mila_observatory.api.diary import router as diary_router
from Mila_observatory.api.needs import router as needs_router
from Mila_observatory.api.users import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    print(f"\n{'='*60}")
    print(f"  Mila Observatory Server")
    print(f"  Config: {RUNTIME_CONFIG_PATH}")
    print(f"  Listen: {resolve_host()}:{settings.OBSERVATORY_PORT} (from .env)")
    print(f"{'='*60}\n")
    # The panel can start before the bot on a fresh install; make sure the
    # shared schema (and the faiss_id indexes/migrations) exists.
    try:
        init_db()
    except Exception as e:
        print(f"[Observatory] DB init failed: {e}")
    yield
    print("\nObservatory server shutting down.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Mila Observatory",
        description="Monitor and control the Mila bot.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS — same-origin panel; no wildcard with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # All API routers require Basic Auth when a password is configured.
    # Health (/api/health) stays public for container checks.
    auth_dep = [Depends(require_auth)]
    app.include_router(dashboard_router, dependencies=auth_dep)
    app.include_router(model_router, dependencies=auth_dep)
    app.include_router(memory_router, dependencies=auth_dep)
    app.include_router(tools_router, dependencies=auth_dep)
    app.include_router(personality_router, dependencies=auth_dep)
    app.include_router(pipeline_router, dependencies=auth_dep)
    app.include_router(config_router, dependencies=auth_dep)
    app.include_router(sleep_router, dependencies=auth_dep)
    app.include_router(diary_router, dependencies=auth_dep)
    app.include_router(needs_router, dependencies=auth_dep)
    app.include_router(users_router, dependencies=auth_dep)

    # Health check
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "mila-observatory"}

    # Try to serve static frontend if built
    frontend_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "Mila_observatory", "frontend", "dist"
    )
    if os.path.exists(frontend_dir):
        # Mount assets directory for JS/CSS/images
        assets_dir = os.path.join(frontend_dir, "assets")
        if os.path.exists(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
        
        # Mount icons directory
        icons_dir = os.path.join(frontend_dir, "icons")
        if os.path.exists(icons_dir):
            app.mount("/icons", StaticFiles(directory=icons_dir), name="icons")
        
        # SPA fallback: serve index.html for all non-API routes.
        # Protected too — the browser shows the native Basic Auth prompt
        # on page open, then reuses cached credentials for /api fetch().
        @app.get("/{full_path:path}", dependencies=[Depends(require_auth)])
        async def spa_fallback(full_path: str):
            # Only serve frontend for paths that are NOT API routes
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            
            file_path = os.path.join(frontend_dir, full_path)
            if os.path.isfile(file_path):
                return FileResponse(file_path)
            # SPA fallback — serve index.html for client-side routing
            return FileResponse(os.path.join(frontend_dir, "index.html"))

    return app


app = create_app()


def main():
    """Entry point for running the server."""
    # Bind address/port come from .env (OBSERVATORY_HOST/OBSERVATORY_PORT),
    # never from runtime_config.json.
    port = settings.OBSERVATORY_PORT
    host = resolve_host()
    if not is_auth_enabled():
        print(
            "[Observatory] WARNING: no OBSERVATORY_PASSWORD set — anyone with "
            "network access to this port controls the bot. Keep it bound to "
            "127.0.0.1 (the default) or set a password."
        )
    print(f"[Observatory] Listening on {host}:{port} (auth={'on' if is_auth_enabled() else 'off'})")
    uvicorn.run(
        "Mila_observatory.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()