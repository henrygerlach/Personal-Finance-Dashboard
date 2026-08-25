"""
FastAPI application entry point with lifecycle management.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_logger
from app.database import db_manager
from app.routes import router
from app.services import ComdirectBalanceService

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifecycle.

    - Startup: Connect to MongoDB
    - Shutdown: Close database connection
    """
    logger.info("Starting application...")
    await db_manager.connect()
    await ComdirectBalanceService.initialize_startup_session()
    yield
    logger.info("Shutting down application...")
    await db_manager.disconnect()


# Initialize FastAPI app with lifespan management
app = FastAPI(
    title="Comdirect Dashboard",
    description="Bank transaction categorizer and analytics dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Include all routes
app.include_router(router)
