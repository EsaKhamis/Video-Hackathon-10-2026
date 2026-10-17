"""Video agent: a plain tool-calling loop on CoreWeave Forge / W&B Inference.

Two entry points:
- handle_event(): an Event from the real-time lane; routed by recommended_agent.
- chat(): a free-form question from the UI; the LLM picks tools.
"""

import json

import weave
from openai import OpenAI

from ..config import settings
from ..reason import cosmos
from ..schema import Clip, Event, append_event, read_events
from ..search.vast import search_footage

SYSTEM = (
    "You help a cyclist turn their helmet-cam and dashcam footage into incident reports about "
    "unsafe drivers: close passes, cut-ins and vehicles blocking the bike lane. "
    "Use search_footage to find candidate clips in the archive, ask_clip to have the vision model "
    "confirm an incident in one clip, send_alert to add a confirmed incident to the review queue, "
    "and write_report to draft reports. Never treat an unconfirmed search hit as an incident. "
    "Cite clip_id and timestamps in answers. Be brief."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_footage",
            "description": "Natural-language search over past clip descriptions.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}, "k": {"type": "integer", "default": 5}},
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clip",
            "description": "Ask the vision model (Cosmos Reason) whether one clip from search_footage shows an incident.",
            "parameters": {
                "type": "object",
                "properties": {"clip_id": {"type": "string"}, "q": {"type": "string"}},
                "required": ["clip_id", "q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_alert",
            "description": "Add a confirmed incident (the Event JSON returned by ask_clip) to the cyclist's review queue.",
            "parameters": {
                "type": "object",
                "properties": {"event": {"type": "object", "description": "Event JSON"}},
                "required": ["event"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_report",
            "description": "Write an incident summary of all events so far.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _llm() -> OpenAI:
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.wandb_api_key,
        project=settings.wandb_project,
    )


def load_clip(clip_id: str) -> Clip:
    meta = settings.clips_dir / f"{clip_id}.json"
    if not meta.exists():
        raise FileNotFoundError(f"Unknown clip_id {clip_id}")
    return Clip.model_validate_json(meta.read_text())


@weave.op()
def send_alert(event: Event) -> str:
    append_event(event)
    return f"alert sent: {event.event} ({event.severity}) on {event.clip_id}"


@weave.op()
def write_report() -> str:
    events = [e for e in read_events() if e.event not in ("ok", "unclear")]
    if not events:
        return "No incidents recorded."
    resp = _llm().chat.completions.create(
        model=settings.llm_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Write a short markdown incident report per event, laid out like a police "
                    "road-incident submission: date/time, incident type, severity, vehicle "
                    "description, summary. Leave 'Plate' and 'Street' as blank fields for the cyclist."
                ),
            },
            {"role": "user", "content": json.dumps([e.model_dump() for e in events])},
        ],
    )
    return resp.choices[0].message.content or ""


def _run_tool(name: str, args: dict) -> str:
    if name == "search_footage":
        return json.dumps(search_footage(args["q"], args.get("k", 5)))
    if name == "ask_clip":
        return cosmos.ask_clip(load_clip(args["clip_id"]), args["q"]).model_dump_json()
    if name == "send_alert":
        return send_alert(Event.model_validate(args["event"]))
    if name == "write_report":
        return write_report()
    return f"unknown tool {name}"


@weave.op()
def chat(question: str, max_steps: int = 12) -> str:
    client = _llm()
    messages: list = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=settings.llm_model, messages=messages, tools=TOOLS
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(msg)
        for call in msg.tool_calls:
            try:
                out = _run_tool(call.function.name, json.loads(call.function.arguments or "{}"))
            except Exception as e:
                out = f"error: {e}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": out})
    return "Stopped after max steps."


@weave.op()
def handle_event(event: Event) -> str:
    if event.recommended_agent == "send_alert":
        return send_alert(event)
    if event.recommended_agent == "write_report":
        send_alert(event)
        return write_report()
    return f"no action for {event.event}"
