"""NewsEngine REST API package."""

from src.api.server import app, create_app
from src.api.routers.events import router as events_router
from src.api.routers.health import router as health_router

__all__ = [
    "create_app",
    "app",
    "events_router",
    "health_router",
]
