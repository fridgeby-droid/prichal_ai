from datetime import date, datetime
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.services.core_directories import get_directories

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/v1/core', tags=['Core directories'])
bearer = HTTPBearer(auto_error=False)
NO_STORE = {'Cache-Control': 'no-store'}


async def require_core_key(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    expected = get_settings().core_directory_api_key.get_secret_value()
    if len(expected) < 32:
        raise HTTPException(503, 'Directory API is not configured', headers=NO_STORE)
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == 'bearer' else ''
    if not supplied or not secrets.compare_digest(supplied.encode('utf-8'), expected.encode('utf-8')):
        raise HTTPException(401, 'Invalid credentials', headers={**NO_STORE, 'WWW-Authenticate': 'Bearer'})


class Store(BaseModel):
    saby_point_id: str
    name: str
    address: str
    locality: str
    updated_at: datetime


class Activity(BaseModel):
    saby_point_id: str
    first_seen: date | None
    last_seen: date | None


class Seller(BaseModel):
    saby_seller_id: str
    display_name: str | None
    observed_names: list[str]
    first_seen: date | None
    last_seen: date | None
    stores: list[Activity]


class Directories(BaseModel):
    schema_version: int
    generated_at: datetime
    last_successful_sync_at: datetime | None
    scope: str
    seller_source: str
    complete_snapshot: bool
    store_count: int
    seller_count: int
    stores: list[Store]
    sellers: list[Seller]


@router.get('/directories', response_model=Directories, dependencies=[Depends(require_core_key)])
async def directories(response: Response):
    """Full snapshot of retail stores and observed accounts; not an HR roster."""
    response.headers['Cache-Control'] = 'no-store'
    try:
        return await get_directories()
    except Exception:
        logger.exception('Core directory export failed')
        raise HTTPException(503, 'Directory data temporarily unavailable', headers=NO_STORE) from None
