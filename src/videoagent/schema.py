"""The JSON contract shared by both lanes, the agent and the UI."""

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .config import settings

AgentTool = Literal["send_alert", "write_report", "ask_clip", "search_footage", "none"]


class Event(BaseModel):
    event: str
    confidence: Literal["low", "medium", "high"] = "medium"
    severity: Literal["info", "warning", "critical"] = "info"
    clip_id: str
    t_start: float
    t_end: float
    reasoning: str = ""
    recommended_agent: AgentTool = "none"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Clip(BaseModel):
    clip_id: str
    source: str
    t_start: float
    t_end: float
    path: str
    yolo_labels: list[str] = []


def append_event(event: Event) -> None:
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.events_path.open("a") as f:
        f.write(event.model_dump_json() + "\n")


def read_events() -> list[Event]:
    if not settings.events_path.exists():
        return []
    lines = settings.events_path.read_text().splitlines()
    return [Event.model_validate(json.loads(line)) for line in lines if line.strip()]
