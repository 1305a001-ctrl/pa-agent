import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Signal(BaseModel):
    """Inbound signal from Redis (matches news-consolidator INTEGRATION.md)."""
    id: UUID
    strategy_id: UUID
    research_config_id: UUID
    strategy_git_sha: str
    research_config_version: int
    asset: str
    direction: Literal["long", "short", "neutral", "watch"]
    confidence: float = Field(ge=0.0, le=1.0)
    composite_risk_score: float | None = None
    risk_score: dict | None = None
    source_article_ids: list[UUID] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)
    published_at: datetime

    @field_validator("risk_score", "payload", mode="before")
    @classmethod
    def _parse_json_string(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v
