import base64

from app.db.database import normalize_postgres_dsn, build_ssl_context
from app.services.storage import _clean_prefix


def test_normalize_postgres_dsn_removes_ssl_params():
    dsn = (
        "postgresql+asyncpg://u:p@host/db?"
        "sslmode=require&channel_binding=require&application_name=x"
    )
    value = normalize_postgres_dsn(dsn)
    assert value.startswith("postgresql://")
    assert "sslmode" not in value
    assert "channel_binding" not in value
    assert "application_name=x" in value


def test_require_ssl_without_ca_does_not_need_root_crt():
    context = build_ssl_context("require")
    assert context is not False
    assert context.check_hostname is False


def test_prefix_isolated():
    assert _clean_prefix("prichal-ai") == "prichal-ai/"
    assert _clean_prefix("/prichal-ai/") == "prichal-ai/"
