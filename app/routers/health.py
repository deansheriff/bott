from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..db import db_ping

router = APIRouter()


@router.get("/health")
def health():
    ok = db_ping()
    return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)
