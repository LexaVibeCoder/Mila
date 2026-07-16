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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from Mila_observatory.runtime import runtime_config, RUNTIME_CONFIG_PATH
from Mila_observatory.api.dashboard import router as dashboard_router
from Mila_observatory.api.model import router as model_router
from Mila_observatory.api.memory import router as memory_router
from Mila_observatory.api.tools import router as tools_router
from Mila_observatory.api.personality import router as personality_router
from Mila_observatory.api.pipeline import router as pipeline_router
from Mila_observatory.api.config import router as config_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    print(f"\n{'='*60}")
    print(f"  Mila Observatory Server")
    print(f"  Config: {RUNTIME_CONFIG_PATH}")
    print(f"  Port: {runtime_config.get('system.observatory_port', 8080)}")
    print(f"{'='*60}\n")
    yield
    print("\nObservatory server shutting down.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Mila Observatory",
        description="Панель управления и наблюдения за агентом Мила",
        version="1.0.0",
        lifespan=lifespan,
    )

    # CORS — allow frontend dev server
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routers
    app.include_router(dashboard_router)
    app.include_router(model_router)
    app.include_router(memory_router)
    app.include_router(tools_router)
    app.include_router(personality_router)
    app.include_router(pipeline_router)
    app.include_router(config_router)

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
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


app = create_app()


def main():
    """Entry point for running the server."""
    port = runtime_config.get("system.observatory_port", 8080)
    uvicorn.run(
        "Mila_observatory.server:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()