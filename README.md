# pa-agent

Phase 8 v0.1 — personal-assistant daemon. First two features:

1. **Signals:critical Telegram alerts** — high-conviction signals (composite risk > 0.85) get pushed instantly with a rich format.
2. **Daily brief** — once a day at `BRIEF_LOCAL_HOUR` (default 08:00 in `BRIEF_TIMEZONE`), summarize the previous 24h and push to Telegram. Optionally polished by the LLM if `LITELLM_API_KEY` is set.

For system context, read [`infra-core/docs/ARCHITECTURE.md`](https://github.com/1305a001-ctrl/infra-core/blob/main/docs/ARCHITECTURE.md) first.

## Module map

```
src/pa_agent/
├── main.py            # asyncio entry — runs critical_loop + brief_loop
├── settings.py        # pydantic-settings; env vars
├── db.py              # asyncpg pool + JSONB codec; READ-ONLY queries
├── models.py          # Signal pydantic type (mirrors news-consolidator)
├── alerts.py          # Telegram delivery + format_critical()
└── brief.py           # 24h aggregation + optional LLM polish
```

`db.py` only reads — pa-agent doesn't own any tables. It joins across `market_signals`, `trades`, `poly_positions`, `pipeline_audit`.

## Risk envelope (defaults; override in env)

| Setting | Default | Env var |
|---|---|---|
| Brief time | 08:00 Asia/Kuala_Lumpur | `BRIEF_LOCAL_HOUR` / `BRIEF_LOCAL_MINUTE` / `BRIEF_TIMEZONE` |
| LLM model | claude-haiku | `LITELLM_MODEL` |
| Halt switch | off | `PA_AGENT_HALT=1` |

`PA_AGENT_HALT=1` skips both critical alerts and the daily brief without restarting the container.

## Future expansions

- Chat UI at `pa.the2357.com` (Telegram bot already covers ad-hoc Q&A)
- Calendar integration (Google / Outlook)
- Email triage
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
