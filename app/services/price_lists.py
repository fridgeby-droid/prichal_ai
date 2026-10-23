"""Saby Retail published assortments. No prices, stock, payroll or LLM calls."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import logging
import re
from time import monotonic
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.services.saby import API_BASE, saby_client


# Per-process, bounded, always revalidated; not a source of stale success.
_image_cache = OrderedDict()
_catalog_gate = asyncio.Lock()
_catalog_next_request = 0.0
logger = logging.getLogger(__name__)


def catalog_interval():
    return max(1.0, min(60.0, get_settings().catalog_min_request_interval_seconds))


def rate_limit_delay(response):
    """Recognize Saby's observed HTTP 500 antibot warning; never log the payload."""
    if response.status_code == 429:
        try:
            return max(120.0, float(response.headers.get('Retry-After', '120')))
        except ValueError:
            return 120.0
    if response.status_code != 500:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get('error') if isinstance(body, dict) else None
    details = error.get('details') if isinstance(error, dict) else None
    if not isinstance(details, str) or 'метод вызывается слишком часто' not in details.lower():
        return None
    match = re.search(r'на\s+(\d+)\s+секунд', details, re.IGNORECASE)
    if not match:
        match = re.search(r'Время блокировки:\s*(\d+)', details, re.IGNORECASE)
    return max(120.0, float(match.group(1))) if match else 120.0


class ExportError(Exception):
    def __init__(self, code="SABY_RESPONSE_INVALID", status=502):
        self.code, self.status = code, status
        super().__init__(code)


def identifier(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise ExportError()
    if int(value) <= 0:
        raise ExportError()
    return str(int(value))


def allowed_point(point):
    point = identifier(point)
    configured = get_settings().saby_price_list_point_ids
    if not configured.strip():
        raise ExportError("PRICE_POINTS_NOT_CONFIGURED", 503)
    try:
        allowed = {identifier(p.strip()) for p in configured.split(",")}
    except ExportError:
        raise ExportError("PRICE_POINTS_CONFIGURATION_INVALID", 503) from None
    if point not in allowed:
        raise ExportError("POINT_NOT_ALLOWED", 403)
    return point


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def page_data(data, key):
    if not isinstance(data, dict) or any(
        data.get(k) not in (None, False, "", [], {}) for k in ("error", "errors", "Error", "Errors")
    ) or data.get("success") is False:
        raise ExportError()
    rows, outcome = data.get(key), data.get("outcome")
    # The official executable examples use outcome.hasMore (both endpoints).
    more = outcome.get("hasMore") if isinstance(outcome, dict) else None
    if not isinstance(rows, list) or type(more) is not bool:
        raise ExportError()
    if any(not isinstance(r, dict) for r in rows) or (more and not rows):
        raise ExportError()
    return rows, more


def text_field(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExportError()
    return value.strip()


def image_locator(value):
    # Canonicalize representations of the same documented Saby endpoint.
    if not isinstance(value, str) or len(value) > 4096:
        raise ExportError("SABY_IMAGE_REFERENCE_INVALID")
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise ExportError("SABY_IMAGE_REFERENCE_INVALID")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ExportError("SABY_IMAGE_REFERENCE_INVALID") from None
    absolute = parts.scheme == "https" and parts.netloc == "api.sbis.ru"
    relative = not parts.scheme and not parts.netloc
    valid_path = (
        absolute and parts.path == "/retail/img"
        # /img?... is observed in the live point 23109 / price 66 catalog.
        or relative and parts.path in {"img", "/img", "/retail/img"}
    )
    if not valid_path or parts.fragment or not parts.query:
        raise ExportError("SABY_IMAGE_REFERENCE_INVALID")
    return "img?" + parts.query


def signing_key():
    key = get_settings().core_price_lists_api_key.get_secret_value()
    if len(key) < 32:
        raise ExportError("PRICE_API_NOT_CONFIGURED", 503)
    return key.encode()


def image_descriptor(locator, point):
    locator = image_locator(locator)
    raw = json.dumps([point, locator], separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(signing_key(), b"price-image-v1:" + payload.encode(), hashlib.sha256).hexdigest()
    return {
        "id": hashlib.sha256(raw).hexdigest(),
        "download_path": f"/api/v1/core/price-list-images/{payload}.{signature}",
        "content_sha256": None,
    }


def decode_image(token):
    if len(token) > 6000:
        raise ExportError("IMAGE_REFERENCE_INVALID", 404)
    try:
        payload, signature = token.split(".")
        expected = hmac.new(signing_key(), b"price-image-v1:" + payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        point, locator = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        allowed_point(point)
        return image_locator(locator)
    except (ValueError, TypeError, UnicodeError, ExportError):
        raise ExportError("IMAGE_REFERENCE_INVALID", 404) from None


async def price_get(path, params):
    """Pace all catalog/price reads in this process and honor upstream cooldowns."""
    global _catalog_next_request
    for attempt in range(3):
        async with _catalog_gate:
            remaining = _catalog_next_request - monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            _catalog_next_request = monotonic() + catalog_interval()
            try:
                return await saby_client._get(path, dict(params))
            except httpx.HTTPStatusError as exc:
                cooldown = rate_limit_delay(exc.response)
                if cooldown is not None:
                    # Keep this gate even after the last attempt so other readers wait.
                    _catalog_next_request = max(_catalog_next_request, monotonic()+cooldown+1)
                    logger.warning('Saby catalog rate limit: wait at least %.0f seconds; attempt %s/3', cooldown+1, attempt+1)
                elif exc.response.status_code == 500:
                    _catalog_next_request = max(_catalog_next_request, monotonic()+2**attempt)
                if (exc.response.status_code != 500 and cooldown is None) or attempt == 2:
                    raise


async def list_prices(point, actual_date=None):
    point = allowed_point(point)
    actual_date = actual_date or datetime.now(ZoneInfo(get_settings().business_tz)).date()
    found = {}
    for page in range(max(1, get_settings().price_lists_max_pages)):
        data = await price_get("/retail/nomenclature/price-list", {
            "pointId": int(point), "actualDate": actual_date.isoformat(),
            "page": page, "pageSize": 25,
        })
        rows, more = page_data(data, "priceLists")
        for row in rows:
            item = {"id": identifier(row.get("id")), "point_id": point, "name": text_field(row, "name")}
            if item["id"] in found:
                raise ExportError("SABY_PAGINATION_OVERLAP")
            found[item["id"]] = item
        if not more:
            items = sorted(found.values(), key=lambda x: x["id"])
            return {"schema_version": 1, "complete_snapshot": True, "fetched_at": now(),
                    "source_updated_at": None, "actual_date": actual_date.isoformat(),
                    "point_id": point, "revision": digest(items), "price_lists": items}
    raise ExportError("SABY_PAGE_LIMIT")


async def get_price(point, price_id):
    point, price_id = allowed_point(point), identifier(price_id)
    listing = await list_prices(point)
    selected = next((p for p in listing["price_lists"] if p["id"] == price_id), None)
    if selected is None:
        raise ExportError("PRICE_LIST_NOT_AVAILABLE", 404)
    params = {"pointId": int(point), "priceListId": int(price_id), "order": "after",
              "pageSize": 25, "noStopList": False, "withBalance": False, "withBarcode": False}
    seen_positions, products = set(), {}
    folders = 0
    for page in range(max(1, get_settings().price_lists_max_pages)):
        data = await price_get("/retail/v2/nomenclature/list", params)
        rows, more = page_data(data, "nomenclatures")
        for row in rows:
            cursor = identifier(row.get("hierarchicalId"))
            if cursor in seen_positions:
                raise ExportError("SABY_PAGINATION_OVERLAP")
            seen_positions.add(cursor)
            # Live Saby Retail response: leaf products have explicit null;
            # folders have true and may have null id/unit/images.
            # Missing markers and non-boolean values remain invalid.
            if "isParent" not in row or (
                row["isParent"] is not None and type(row["isParent"]) is not bool
            ):
                raise ExportError()
            if row["isParent"] is True:
                folders += 1
                continue
            images = row.get("images", [])
            if images is None:
                images = []
            if not isinstance(images, list):
                raise ExportError()
            # Validate before deduplication, including non-string references.
            images = [image_locator(image) for image in images]
            item = {"id": identifier(row.get("id")), "name": text_field(row, "name"),
                    "unit": text_field(row, "unit"),
                    "images": [image_descriptor(image, point) for image in dict.fromkeys(images)]}
            old = products.get(item["id"])
            if old is not None and old != item:
                raise ExportError("SABY_PRODUCT_CONFLICT")
            products[item["id"]] = item
        if not more:
            # Confirm that the published price did not vanish during traversal.
            check = await list_prices(point)
            if selected not in check["price_lists"]:
                raise ExportError("SABY_PRICE_CHANGED_DURING_EXPORT")
            items = sorted(products.values(), key=lambda x: x["id"])
            return {"schema_version": 1, "complete_snapshot": True, "fetched_at": now(),
                    "source_updated_at": None, "actual_date": listing["actual_date"],
                    "price_list": selected, "revision": digest([selected, items]),
                    "product_count": len(items), "folders_skipped": folders,
                    "products": items}
        params["position"] = int(rows[-1]["hierarchicalId"])
    raise ExportError("SABY_PAGE_LIMIT")


async def download_image(reference):
    locator = decode_image(reference)
    cached = _image_cache.get(locator)
    headers = {}
    if cached:
        if cached[3]:
            headers["If-None-Match"] = cached[3]
        elif cached[4]:
            headers["If-Modified-Since"] = cached[4]
    # Bound memory before reading. Never forward credentials on a redirect.
    for attempt in range(2):
        token = await saby_client._authenticate(force=attempt == 1)
        async with saby_client._requests:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                async with client.stream("GET", API_BASE + "/retail/" + locator,
                                         headers={**headers, "X-SBISAccessToken": token}) as response:
                    if response.status_code in (401, 403) and attempt == 0:
                        continue
                    if response.status_code == 304 and cached and headers:
                        return cached[:3]
                    if response.status_code != 200:
                        raise ExportError("SABY_IMAGE_UNAVAILABLE")
                    mime = response.headers.get("content-type", "").split(";")[0].lower()
                    if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                        raise ExportError("SABY_IMAGE_TYPE_INVALID")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 10 * 1024 * 1024:
                            raise ExportError("SABY_IMAGE_TOO_LARGE")
                    raw = bytes(content)
                    valid = (
                        mime == "image/jpeg" and raw.startswith(b"\xff\xd8\xff") or
                        mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n") or
                        mime == "image/gif" and raw[:6] in (b"GIF87a", b"GIF89a") or
                        mime == "image/webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
                    )
                    if not valid:
                        raise ExportError("SABY_IMAGE_TYPE_INVALID")
                    result = (raw, mime, hashlib.sha256(raw).hexdigest())
                    _image_cache[locator] = (*result, response.headers.get("etag"),
                                             response.headers.get("last-modified"))
                    _image_cache.move_to_end(locator)
                    while (len(_image_cache) > 32 or
                           sum(len(entry[0]) for entry in _image_cache.values()) > 32*1024*1024):
                        _image_cache.popitem(last=False)
                    return result
    raise ExportError("SABY_IMAGE_UNAVAILABLE")
