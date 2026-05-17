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

    # Poly settlement-alert subscriber (Phase 8 v0.7). Reads poly-adapter
    # v0.2 settlement events (one XADD per auto-closed resolved market)
    # and forwards each to Telegram with the booked PnL.
    poly_settlement_alerts_stream: str = "poly:settlement_alerts"

    # Alpha fire alerts (Phase 8 v0.8). Subscribes to alphas:active stream,
    # filters by strategy_slug, sends one Telegram message per fire.
    # Default-enabled because operator visibility is high-value with no
    # cost (the strategies fire infrequently; Telegram noise is bounded).
    alpha_alerts_enabled: bool = True
    alphas_stream: str = "alphas:active"
    # CSV of strategy slugs to alert on. Default tracks the active poly
    # strategies. Empty = all strategies (noisy).
    alpha_alert_strategies: str = (
        "poly-fade-extreme,poly-liq-cascade-taker-daily,"
        "poly-fade-daily,poly-liq-cascade-taker,poly-sell-wings,"
        "poly-news-taker,poly-news-thesis-taker,"
        "poly-resolution-momentum,poly-momentum-slow,"
        "poly-crypto-momentum-slow,poly-politics-momentum-slow,"
        "poly-macro-momentum-slow"
    )

    # EOD daily digest (Phase 8 v0.9). Posts a per-strategy paper PnL
    # summary to Telegram at brief_local_hour - useful operator habit.
    eod_digest_enabled: bool = True
    # Strategies to include in the digest (CSV). Empty = all strategies
    # that have positions in the lookback window.
    eod_digest_strategies: str = ""

    # Auto-disable monitor (Phase 8 v0.9). Watches per-strategy realized
    # PnL on closed positions; halts when consecutive losses or 24h
    # drawdown breaches the threshold. Adds halted slug to Redis set
    # `strategy:halts` and sends a Telegram alert. Operator can manually
    # SREM to un-halt or flip frontmatter to inactive permanently.
    auto_disable_enabled: bool = True
    auto_disable_check_interval_sec: int = 300  # 5 min
    auto_disable_consecutive_loss_threshold: int = 5
    auto_disable_realized_24h_threshold: float = -100.0  # halts when 24h ≤ -$100
    auto_disable_redis_halt_set: str = "strategy:halts"
    # Strategies to monitor (CSV). Empty defaults to known poly strategies.
    auto_disable_strategies: str = (
        "poly-fade-extreme,poly-liq-cascade-taker-daily,"
        "poly-fade-daily,poly-liq-cascade-taker,poly-sell-wings,"
        "poly-news-taker,poly-news-thesis-taker,"
        "poly-resolution-momentum,"
        "poly-crypto-momentum,poly-crypto-momentum-slow,"
        "poly-politics-momentum-slow,poly-macro-momentum-slow"
    )

    # Cap-breach subscriber (Phase 8 v0.11). Reads the oms-gateway
    # `risk:cap_breaches` stream and forwards a Telegram alert per breach.
    # Rate-limited per (reason, cluster) tuple so a runaway pulse doesn't
    # spam — first breach in a window pings, rest are absorbed silently.
    cap_breach_alerts_enabled: bool = True
    cap_breach_alerts_stream: str = "risk:cap_breaches"
    cap_breach_alert_rate_limit_sec: int = 1800  # 30 min per (reason, key)

    # Position-aging monitor (Phase 8 v0.10). Periodically scans open
    # positions; sends one Telegram alert per (position, age-tier) crossing
    # so the operator sees stale opens without re-alerts every cycle.
    # Polymarket positions are skipped (resolution-bound by design).
    position_aging_enabled: bool = True
    position_aging_check_interval_sec: int = 1800  # 30 min
    # Sent-alert keys live in this Redis set with a TTL so stale entries
    # don't accumulate forever after a position closes.
    position_aging_redis_set: str = "position:aging_alerts_sent"
    position_aging_alert_ttl_sec: int = 60 * 60 * 24 * 30  # 30 days

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
