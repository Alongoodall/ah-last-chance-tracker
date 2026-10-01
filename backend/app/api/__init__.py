"""API package — assembles all routers into one include."""

from fastapi import APIRouter

from app.api.routes.bargains import router as bargains_router
from app.api.routes.products import router as products_router
from app.api.routes.stores import router as stores_router

# Single router to include in create_app()
api_router = APIRouter()
api_router.include_router(stores_router)
api_router.include_router(bargains_router)
api_router.include_router(products_router)

__all__ = ["api_router"]
