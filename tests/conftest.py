import os

# Test imports must never require or use deployment secrets.
for key, value in {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "TELEGRAM_ALLOWED_USER_IDS": "1",
    "OPENAI_API_KEY": "test",
    "SABY_APP_CLIENT_ID": "test",
    "SABY_APP_SECRET": "test",
    "SABY_SECRET_KEY": "test",
    "DATABASE_URL": "postgresql://test:test@127.0.0.1/test",
    "AUTO_SYNC_ENABLED": "false",
    "AUTO_SYNC_ON_START": "false",
}.items():
    os.environ[key] = value
