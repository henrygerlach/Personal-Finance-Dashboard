"""
FastAPI application entry point with lifecycle management.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AuthRequired
from app.config import get_logger, settings
from app.database import db_manager
from app.routes import public_router, router

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
    yield
    logger.info("Shutting down application...")
    await db_manager.disconnect()


# Initialize FastAPI app with lifespan management
app = FastAPI(
    title="Personal Finance Dashboard",
    description="Bank transaction categorizer and analytics dashboard",
    version="0.1.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)


@app.exception_handler(AuthRequired)
async def handle_auth_required(request: Request, exc: AuthRequired) -> Response:
    """No session: send an API caller a 401, and a page to the login form."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return RedirectResponse(url=f"/login?next={request.url.path}")


# Include all routes
app.include_router(public_router)
app.include_router(router)
