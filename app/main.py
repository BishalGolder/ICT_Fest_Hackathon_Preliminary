from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .database import Base, engine
from .errors import AppError
from .routers import admin, auth, bookings, rooms

app = FastAPI()

# 1. Create database tables on startup (fixes "no such table" errors)
Base.metadata.create_all(bind=engine)

# 2. Custom Exception Handler for AppError
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code}
    )

# 3. Global Catch-All Handler (Prevents unhandled crashes from escaping the JSON contract)
@app.exception_handler(Exception)
async def global_fallback_handler(request: Request, exc: Exception):
    print(f"CRITICAL SYSTEM UNHANDLED FAILURE: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal system error occurred", "code": "INTERNAL_SERVER_ERROR"}
    )

# 4. Health Check (required by smoke tests)
@app.get("/health")
def health_check():
    return {"status": "ok"}

# 5. Include All App Routers (including admin, which was previously missing)
app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(rooms.router)
app.include_router(admin.router)