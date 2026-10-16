# Video-Hackathon-10-2026

Real-time video agent for the VAST Builders Challenge (London, Sat 17 Oct). Architecture and rules for the day are in [video-agent-stack.md](video-agent-stack.md).

Two lanes feed one agent:

- **Search lane**: footage is cut into clips, each clip is described with our ingest prompt (VAST + Cosmos), and the descriptions are searchable in natural language.
- **Real-time lane**: footage is replayed at 2–5 fps, YOLO11 tracks every frame, and only clips YOLO flags go to Cosmos Reason 2, which returns a JSON event.
- **Agent**: an LLM on CoreWeave Forge / W&B Inference with tools `search_footage`, `ask_clip`, `send_alert`, `write_report`. Every step is traced in Weave.
- **UI**: Streamlit with a live player, alert feed, Cosmos reasoning, search and agent chat.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # fill in keys and endpoints
```

Put footage in `data/footage/` and point `VIDEO_SOURCE` at it.

## Run

```bash
# UI (live player, alerts, search, chat)
uv run streamlit run app.py

# Real-time lane headless; --no-cosmos to tune YOLO triggers only, --fast to skip real-time pacing
uv run scripts/run_realtime.py data/footage/cam1.mp4 --no-cosmos

# Build the local search index from footage (until VAST search is wired up)
uv run scripts/ingest.py data/footage/*.mp4
```

## Where things live

| Path | What |
|---|---|
| `src/videoagent/schema.py` | The JSON event contract shared by both lanes |
| `src/videoagent/prompts/ingest.txt` | Ingest prompt: decides what search can find. Write this first |
| `src/videoagent/prompts/cosmos.txt` | Cosmos prompt for the use case; returns the event JSON |
| `src/videoagent/realtime/pipeline.py` | OpenCV replay, YOLO11 tracking, trigger rule, clip writer |
| `src/videoagent/reason/cosmos.py` | Cosmos Reason 2 client (OpenAI-compatible endpoint) |
| `src/videoagent/search/vast.py` | VAST search client, with a local JSON fallback |
| `src/videoagent/agent/agent.py` | Tool-calling agent loop and tools |
| `app.py` | Streamlit UI |

Runtime state goes to `data/` (gitignored): flagged clips in `data/clips/`, alerts in `data/events.jsonl`, local index in `data/index.json`.

## Build-day checklist

- [ ] Fill in `.env`: W&B key, Cosmos endpoint, VAST endpoint
- [ ] Look at the footage, pick one use case, rewrite both prompts for it
- [ ] Set `TRIGGER_CLASSES` for the YOLO filter
- [ ] Match `search/vast.py` to the real VAST search API
- [ ] Stop features at 3:30, record demo by 4:00, submit by 4:30

## Submission

- Team: _names + emails_
- Demo video: _link_
- Tools: VAST, NVIDIA Cosmos Reason 2, Ultralytics YOLO11, CoreWeave Forge / W&B Inference, W&B Weave, Streamlit, Cursor
