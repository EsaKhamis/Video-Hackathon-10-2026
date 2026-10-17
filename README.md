# Video-Hackathon-10-2026

Real-time video agent for the VAST Builders Challenge (London, Sat 17 Oct). Architecture and rules for the day are in [video-agent-stack.md](video-agent-stack.md).

Two lanes feed one agent:

- **Search lane**: footage is cut into clips, each clip is described with our ingest prompt (VAST + Cosmos), and the descriptions are searchable in natural language.
- **Real-time lane**: footage is replayed at 2–5 fps, YOLO11 tracks every frame, and only clips YOLO flags go to Cosmos Reason 2, which returns a JSON event.
- **Agent**: an LLM on CoreWeave Forge / W&B Inference with tools `search_footage`, `ask_clip`, `send_alert`, `write_report`. Every step is traced in Weave.
- **UI**: Streamlit with a live player, alert feed, Cosmos reasoning, search and agent chat.

Use case: **"Your helmet cam is a witness"**. Find close passes, cut-ins and blocked bike lanes
in cyclists' GoPro/dashcam footage and draft reports the cyclist approves. See [INITIAL_IDEA.md](INITIAL_IDEA.md).

## Setup (team VM)

Requires [uv](https://docs.astral.sh/uv/) (already installed on the VM).

```bash
uv sync
cp .env.example .env   # set COSMOS_BASE_URL; everything else comes from the VM's environment
```

The VM already exports the W&B key/project/team, the VSS backend URL and login, and the GPU bearer
token; `config.py` reads them directly. Search runs against the hosted VSS archive and Cosmos runs
on the hosted `nvidia/cosmos3-nano-reasoner` NIM (max 5 frames per request).

## Run

```bash
# UI (search + Cosmos check, agent chat, alert feed)
uv run streamlit run app.py
```

Cursor forwards the port automatically; open the link it offers (or `http://localhost:8501`).

Optional local real-time lane (pulls in PyTorch; the hosted archive already has YOLO detections):

```bash
uv sync --extra realtime
uv run scripts/run_realtime.py data/footage/cam1.mp4 --no-cosmos
```

`probe/` holds the earlier exploration scripts against the same API.

## Where things live

| Path | What |
|---|---|
| `src/videoagent/schema.py` | The JSON event contract shared by both lanes |
| `src/videoagent/prompts/ingest.txt` | Ingest prompt: decides what search can find. Write this first |
| `src/videoagent/prompts/cosmos.txt` | Cosmos prompt for the use case; returns the event JSON |
| `src/videoagent/realtime/pipeline.py` | OpenCV replay, YOLO11 tracking, trigger rule, clip writer |
| `src/videoagent/reason/cosmos.py` | Cosmos Reason 2 client (OpenAI-compatible endpoint) |
| `src/videoagent/search/vast.py` | VSS retrieval client (login, `/api/v1/search`, clip download), with a local JSON fallback |
| `src/videoagent/agent/agent.py` | Tool-calling agent loop and tools |
| `app.py` | Streamlit UI |

Runtime state goes to `data/` (gitignored): flagged clips in `data/clips/`, alerts in `data/events.jsonl`, local index in `data/index.json`.

## Build-day checklist

- [x] Fill in `.env`: W&B key, Cosmos endpoint, VAST endpoint
- [x] Look at the footage, pick one use case, rewrite both prompts for it
- [x] Match `search/vast.py` to the real VAST search API
- [ ] Review queue: approve / edit / dismiss incidents, export report
- [ ] Stop features at 3:30, record demo by 4:00, submit by 4:30

## Submission

- Team: _names + emails_
- Demo video: _link_
- Tools: VAST, NVIDIA Cosmos Reason 2, Ultralytics YOLO11, CoreWeave Forge / W&B Inference, W&B Weave, Streamlit, Cursor
