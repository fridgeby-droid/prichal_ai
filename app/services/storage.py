from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath

import boto3
from botocore.config import Config

from app.config import get_settings


settings = get_settings()


def _clean_prefix(value: str) -> str:
    value = (value or "").strip().strip("/")
    return f"{value}/" if value else ""


@dataclass(slots=True)
class S3Storage:
    def enabled(self) -> bool:
        return bool(
            settings.s3_enabled
            and settings.s3_bucket.strip()
            and settings.s3_access_key_id.strip()
            and settings.s3_secret_access_key.strip()
        )

    @property
    def prefix(self) -> str:
        return _clean_prefix(settings.s3_prefix)

    def _client(self):
        return boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url.strip(),
            aws_access_key_id=settings.s3_access_key_id.strip(),
            aws_secret_access_key=settings.s3_secret_access_key.strip(),
            region_name=settings.s3_region.strip() or "ru-1",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def key(self, *parts: str) -> str:
        safe_parts = []
        for part in parts:
            text = str(part).strip().strip("/")
            if text:
                safe_parts.append(text)
        suffix = str(PurePosixPath(*safe_parts)) if safe_parts else ""
        return self.prefix + suffix

    async def health(self) -> dict:
        if not settings.s3_enabled:
            return {
                "ok": True,
                "enabled": False,
                "provider": "timeweb-s3",
                "prefix": self.prefix,
            }

        if not self.enabled():
            return {
                "ok": False,
                "enabled": True,
                "provider": "timeweb-s3",
                "error": "S3 is enabled but bucket/access credentials are incomplete",
                "prefix": self.prefix,
            }

        def probe():
            client = self._client()
            result = client.list_objects_v2(
                Bucket=settings.s3_bucket.strip(),
                Prefix=self.prefix,
                MaxKeys=1,
            )
            return int(result.get("KeyCount", 0))

        try:
            key_count = await asyncio.to_thread(probe)
            return {
                "ok": True,
                "enabled": True,
                "provider": "timeweb-s3",
                "endpoint": settings.s3_endpoint_url.strip(),
                "bucket": settings.s3_bucket.strip(),
                "region": settings.s3_region.strip(),
                "prefix": self.prefix,
                "probe_key_count": key_count,
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "provider": "timeweb-s3",
                "endpoint": settings.s3_endpoint_url.strip(),
                "bucket": settings.s3_bucket.strip(),
                "prefix": self.prefix,
                "error": f"{type(exc).__name__}: {exc}",
            }

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> str:
        if not self.enabled():
            raise RuntimeError("S3 storage is not configured")

        object_key = self.key(key)

        def upload():
            self._client().put_object(
                Bucket=settings.s3_bucket.strip(),
                Key=object_key,
                Body=data,
                ContentType=content_type,
                Metadata=metadata or {},
            )

        await asyncio.to_thread(upload)
        return object_key

    async def get_bytes(self, key: str) -> bytes:
        if not self.enabled():
            raise RuntimeError("S3 storage is not configured")

        object_key = self.key(key)

        def download():
            result = self._client().get_object(
                Bucket=settings.s3_bucket.strip(),
                Key=object_key,
            )
            return result["Body"].read()

        return await asyncio.to_thread(download)


storage = S3Storage()
