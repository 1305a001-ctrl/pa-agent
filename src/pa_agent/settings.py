from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database + Redis
    aicore_db_url: str = ""
    redis_url: str = "redis://localhost:6379"

    # Daily-brief schedule (hour:minute, local timezone)
    brief_local_hour: int = 8
    brief_local_minute: int = 0
    brief_timezone: str = "Asia/Kuala_Lumpur"

    # LLM (used to summarize the daily brief)
    litellm_base_url: str = "http://litellm:4000"
    litellm_api_key: str = ""
    litellm_model: str = "claude-haiku"

    # Telegram (required for alerts and brief)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Sentry
    sentry_dsn: str = ""

    # Kill switch
    pa_agent_halt: bool = False

    # CommandCenter context — Mac-side personal-life workspace cloned read-only on ai-primary.
    # When set + path exists, brief_loop reads recent memory entries and injects them into
    # the LLM polish prompt so the daily brief reflects Ben's evolving context.
    commandcenter_path: str = "/home/benadmin/commandcenter"
    commandcenter_memory_entries: int = 5


settings = Settings()
