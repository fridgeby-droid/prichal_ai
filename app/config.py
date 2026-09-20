from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_allowed_user_ids: str

    openai_api_key: str
    openai_default_model: str = "gpt-5.6-luna"

    saby_app_client_id: str
    saby_app_secret: str
    saby_secret_key: str
    saby_point_ids: str = ""
    saby_max_pages_per_point: int = 100

    # Neon / PostgreSQL
    database_url: str
    auto_sync_enabled: bool = True
    auto_sync_on_start: bool = True
    sync_interval_minutes: int = 60
    sync_recent_days: int = 3
    max_manual_sync_days: int = 60

    # ShiftEngine — перенесено из проверенной логики Apps Script.
    shift_day_start_hour: int = 8
    shift_night_start_hour: int = 20
    shift_session_gap_hours: float = 9.0
    shift_max_duration_hours: float = 16.0
    shift_auto_share: float = 0.80
    shift_ambiguous_share: float = 0.65

    core_base_url: str = ""
    core_api_token: str = ""
    core_health_path: str = "/health"

    business_tz: str = "Asia/Yekaterinburg"
    port: int = 8080
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def allowed_user_ids(self) -> set[int]:
        result: set[int] = set()
        for item in self.telegram_allowed_user_ids.replace(";", ",").split(","):
            item = item.strip()
            if item:
                result.add(int(item))
        return result

    @property
    def saby_points_filter(self) -> set[int]:
        result: set[int] = set()
        for item in self.saby_point_ids.replace(";", ",").split(","):
            item = item.strip()
            if item:
                result.add(int(item))
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
