from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import AppError
# Import your routers here (adjust these import names if your project folders are named slightly differently)
from .routers import auth, bookings, rooms

app = FastAPI()

# 1. Custom Exception Handler for AppError
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code}
    )

# 2. Global Catch-All Handler (Prevents unhandled crashes from escaping the JSON contract)
@app.exception_handler(Exception)
async def global_fallback_handler(request: Request, exc: Exception):
    print(f"CRITICAL SYSTEM UNHANDLED FAILURE: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal system error occurred", "code": "INTERNAL_SERVER_ERROR"}
    )

# 3. The Missing Health Check Route (What the smoke test is looking for!)
@app.get("/health")
def health_check():
    return {"status": "ok"}

# 4. Include All App Routers
app.include_router(auth.router)
app.include_router(bookings.router)
app.include_router(rooms.router)