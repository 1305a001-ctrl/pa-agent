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

    # Correlation-alert subscriber (Phase 8 v0.2). Reads risk-watcher v0.9
    # transition events from this redis stream and forwards to Telegram.
    correlation_alerts_stream: str = "risk:correlation_alerts"

    # CommandCenter context — Mac-side personal-life workspace cloned read-only on ai-primary.
    # When set + path exists, brief_loop reads recent memory entries and injects them into
    # the LLM polish prompt so the daily brief reflects Ben's evolving context.
    commandcenter_path: str = "/home/benadmin/commandcenter"
    commandcenter_memory_entries: int = 5

    # Gmail OAuth (Phase 8 v0.6 — Option C2 auto-pull).
    # Empty = inbox loop dormant; loop kicks in once all three are set.
    # Setup walkthrough in pa-agent/README.md#gmail-oauth.
    gmail_oauth_client_id: str = ""
    gmail_oauth_client_secret: str = ""
    gmail_oauth_refresh_token: str = ""
    # Gmail search query — only messages matching this get triaged. Default
    # pulls unread INBOX items; tighten via env if you want a more curated feed.
    gmail_query: str = "is:unread in:inbox"
    # Loop cadence — Gmail API quota is generous (1B/day) so 5 min is fine.
    # The loop pulls + triages + marks-as-read in one tick.
    gmail_poll_interval_sec: int = 300
    # Cap per tick so a flood doesn't spam Telegram. Excess waits for next tick.
    gmail_max_messages_per_tick: int = 10
    # When triage urgency >= this level, push the summary to Telegram.
    # Lower-urgency items just land in _inbox/triage-YYYY-MM-DD.md silently.
    gmail_telegram_min_urgency: str = "high"  # 'low' | 'medium' | 'high'


settings = Settings()
