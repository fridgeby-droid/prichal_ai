"""Server-to-server API; deliberately independent from the directories key."""
import asyncio
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.services import price_lists as service

router = APIRouter(prefix="/api/v1/core", tags=["Core price lists"])
bearer = HTTPBearer(auto_error=False)


async def require_price_key(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    key = get_settings().core_price_lists_api_key.get_secret_value()
    if len(key) < 32:
        raise HTTPException(503, "Price API is not configured", headers={"Cache-Control": "no-store"})
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not supplied or not secrets.compare_digest(supplied.encode(), key.encode()):
        raise HTTPException(401, "Invalid credentials",
                            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})


def failed(code, status):
    return JSONResponse({"schema_version": 1, "complete_snapshot": False, "error": {"code": code}},
                        status_code=status, headers={"Cache-Control": "no-store"})


async def export(call):
    try:
        async with asyncio.timeout(max(1, get_settings().price_lists_timeout_seconds)):
            value = await call
        return JSONResponse(value, headers={"Cache-Control": "no-store"})
    except service.ExportError as exc:
        return failed(exc.code, exc.status)
    except TimeoutError:
        return failed("SABY_EXPORT_TIMEOUT", 504)
    except Exception:
        # Do not serialize/log HTTP exceptions that may include source image URLs.
        return failed("SABY_UNAVAILABLE", 502)


@router.get("/price-lists", dependencies=[Depends(require_price_key)])
async def price_lists(point_id: str = Query(pattern=r"^[1-9][0-9]{0,19}$")):
    return await export(service.list_prices(point_id))


@router.get("/price-lists/{price_list_id}", dependencies=[Depends(require_price_key)])
async def price_list(price_list_id: str, point_id: str = Query(pattern=r"^[1-9][0-9]{0,19}$")):
    return await export(service.get_price(point_id, price_list_id))


@router.get("/price-list-images/{reference}", dependencies=[Depends(require_price_key)])
async def image(reference: str, if_none_match: str | None = Header(default=None)):
    try:
        async with asyncio.timeout(65):
            raw, mime, checksum = await service.download_image(reference)
        etag = f'"{checksum}"'
        headers = {"ETag": etag, "X-Content-SHA256": checksum,
                   "Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff",
                   "Vary": "Authorization", "Content-Disposition": "attachment"}
        matches = {s.strip().removeprefix("W/") for s in (if_none_match or "").split(",")}
        if etag in matches or "*" in matches:
            return Response(status_code=304, headers=headers)
        return Response(raw, media_type=mime, headers=headers)
    except service.ExportError as exc:
        return failed(exc.code, exc.status)
    except TimeoutError:
        return failed("SABY_IMAGE_TIMEOUT", 504)
    except Exception:
        return failed("SABY_IMAGE_UNAVAILABLE", 502)
