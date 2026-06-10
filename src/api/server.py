"""FastAPI 应用工厂 — NewsEngine REST API (:8100)。"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.core.config import get_settings
from src.api.routers.events import router as events_router
from src.api.routers.health import router as health_router


def create_app() -> FastAPI:
    """创建 FastAPI 应用（工厂模式，便于测试）。"""
    settings = get_settings()

    app = FastAPI(
        title="NewsEngine",
        version="1.0.0",
        description="AI-powered financial event intelligence engine",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — 允许 SynapseEngine 跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{settings.api_port}",
            "http://localhost:8000",   # SynapseEngine
            "http://localhost:3000",   # SynapseUI (Next.js)
        ],
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(events_router)
    app.include_router(health_router)

    return app


app = create_app()
