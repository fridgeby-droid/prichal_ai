from __future__ import annotations

import httpx

from app.config import get_settings


class CoreClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def health(self) -> dict:
        base = self.settings.core_base_url.strip().rstrip("/")
        if not base:
            return {
                "configured": False,
                "message": "CORE_BASE_URL пока не настроен.",
            }

        headers = {}
        if self.settings.core_api_token:
            headers["Authorization"] = f"Bearer {self.settings.core_api_token}"

        url = f"{base}{self.settings.core_health_path}"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, headers=headers)

            return {
                "configured": True,
                "url": url,
                "http_status": response.status_code,
                "ok": 200 <= response.status_code < 300,
                "body_preview": response.text[:500],
            }
        except Exception as exc:
            return {
                "configured": True,
                "url": url,
                "ok": False,
                "error": str(exc),
            }


core_client = CoreClient()
