# pa-agent

Phase 8 — personal-assistant daemon.

**v0.1 features (live)**

1. **Signals:critical Telegram alerts** — high-conviction signals (composite risk > 0.85) get pushed instantly with a rich format.
2. **Daily brief** — once a day at `BRIEF_LOCAL_HOUR` (default 08:00 in `BRIEF_TIMEZONE`), summarize the previous 24h and push to Telegram. Optionally polished by the LLM if `LITELLM_API_KEY` is set.

**v0.2 features (live)**

3. **Telegram bot commands** — DM the bot from your phone. Authorized only for the configured `TELEGRAM_CHAT_ID`; every other chat is silently ignored.
   - `/help`   — list commands
   - `/status` — open positions + last pipeline run
   - `/brief`  — fire the daily brief on demand
   - `/ping`   — health check

For system context, read [`infra-core/docs/ARCHITECTURE.md`](https://github.com/1305a001-ctrl/infra-core/blob/main/docs/ARCHITECTURE.md) first.

## Module map

```
src/pa_agent/
├── main.py            # asyncio entry — runs critical_loop + brief_loop + bot_loop
├── settings.py        # pydantic-settings; env vars
├── db.py              # asyncpg pool + JSONB codec; READ-ONLY queries
├── models.py          # Signal pydantic type (mirrors news-consolidator)
├── alerts.py          # Telegram delivery + format_critical()
├── brief.py           # 24h aggregation + optional LLM polish
└── bot.py             # Telegram long-polling bot — dispatches /commands
```

`db.py` only reads — pa-agent doesn't own any tables. It joins across `market_signals`, `trades`, `poly_positions`, `pipeline_audit`.

## Risk envelope (defaults; override in env)

| Setting | Default | Env var |
|---|---|---|
| Brief time | 08:00 Asia/Kuala_Lumpur | `BRIEF_LOCAL_HOUR` / `BRIEF_LOCAL_MINUTE` / `BRIEF_TIMEZONE` |
| LLM model | claude-haiku | `LITELLM_MODEL` |
| Halt switch | off | `PA_AGENT_HALT=1` |

`PA_AGENT_HALT=1` skips both critical alerts and the daily brief without restarting the container.

## Gmail OAuth — auto-pull /inbox triage

The `inbox_pull_loop` (5th concurrent loop) auto-pulls unread Gmail messages, triages each via the existing `triage_inbox_text` LLM helper, archives to `_inbox/triage-YYYY-MM-DD.md`, and pushes high-urgency items to Telegram.

**Stays dormant until all three OAuth env vars are set** — safe to ship without breaking existing manual-forward `/inbox` behaviour.

### One-time setup (Ben)

1. **Create a Google Cloud project** at https://console.cloud.google.com (or pick existing).
2. **Enable Gmail API**: APIs & Services → Library → search "Gmail API" → Enable.
3. **Configure OAuth consent screen**: APIs & Services → OAuth consent screen → External → fill in app name + your email → add scope `https://www.googleapis.com/auth/gmail.modify` → add yourself as test user.
4. **Create OAuth client ID**: Credentials → "Create credentials" → OAuth client ID → application type "Desktop app" → name "pa-agent-inbox" → save.
5. **Download** the client_id + client_secret.
6. **Generate a refresh token** (one-time, on Mac):
   ```bash
   pip install google-auth-oauthlib
   python3 - <<'PY'
   from google_auth_oauthlib.flow import InstalledAppFlow
   flow = InstalledAppFlow.from_client_config(
       {"installed": {
           "client_id": "<CLIENT_ID>",
           "client_secret": "<CLIENT_SECRET>",
           "redirect_uris": ["http://localhost"],
           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
           "token_uri": "https://oauth2.googleapis.com/token",
       }},
       scopes=["https://www.googleapis.com/auth/gmail.modify"],
   )
   creds = flow.run_local_server(port=0)
   print("REFRESH TOKEN:", creds.refresh_token)
   PY
   ```
   Sign in in the browser, grant access, copy the printed refresh token.
7. **Provision pa-agent.env on ai-primary**:
   ```
   GMAIL_OAUTH_CLIENT_ID=...
   GMAIL_OAUTH_CLIENT_SECRET=...
   GMAIL_OAUTH_REFRESH_TOKEN=...
   GMAIL_TELEGRAM_MIN_URGENCY=high
   ```
8. `docker compose up -d pa-agent` — the loop wakes up and starts pulling within `gmail_poll_interval_sec` (default 5 min).

### Tunables

| Env | Default | Notes |
|---|---|---|
| `GMAIL_QUERY` | `is:unread in:inbox` | Standard Gmail search syntax |
| `GMAIL_POLL_INTERVAL_SEC` | `300` | 5 min cadence; Gmail daily quota is 1B requests |
| `GMAIL_MAX_MESSAGES_PER_TICK` | `10` | Cap so a flood doesn't spam Telegram |
| `GMAIL_TELEGRAM_MIN_URGENCY` | `high` | Only triages above this level get pushed to Telegram |

### Deactivate

Clear any one OAuth env var → reload pa-agent → loop goes dormant. The other 4 loops continue.

## Future expansions

- Chat UI at `pa.the2357.com` (Telegram bot already covers ad-hoc Q&A)
- Calendar integration (Google / Outlook)
- Task management

These will be added as `src/pa_agent/<feature>.py` modules and wired into `main.py` as additional concurrent loops. New features must remain optional — pa-agent should keep working with just Postgres + Redis + Telegram.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

`tests/` covers: critical-alert formatting, brief aggregation logic, scheduling math.

## Wire-up

1. No migration — pa-agent owns no tables.
2. Set env at `/srv/secrets/pa-agent.env` (Postgres, Redis, Telegram, optional LiteLLM).
3. `docker compose -f infra-core/compose/pa-agent/docker-compose.yml up -d`
4. (Optional) Hand-test critical alerts by republishing any high-risk signal on `signals:critical`.

See [LOCAL-DEV.md](https://github.com/1305a001-ctrl/infra-core/blob/main/docs/LOCAL-DEV.md).
