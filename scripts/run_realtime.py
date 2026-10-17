"""Headless real-time lane: YOLO -> Cosmos -> agent -> data/events.jsonl.

    uv run scripts/run_realtime.py [VIDEO] [--fast] [--no-cosmos]
"""

import argparse

from videoagent import tracing
from videoagent.agent.agent import handle_event
from videoagent.config import settings
from videoagent.realtime.pipeline import run
from videoagent.reason.cosmos import ask_clip

parser = argparse.ArgumentParser()
parser.add_argument("source", nargs="?", default=settings.video_source)
parser.add_argument("--fast", action="store_true", help="don't sleep between frames")
parser.add_argument("--no-cosmos", action="store_true", help="only print YOLO flags")
args = parser.parse_args()

tracing.init()
for t, _, clip in run(args.source, realtime=not args.fast):
    if clip is None:
        continue
    print(f"[{t:7.1f}s] flagged {clip.clip_id} labels={clip.yolo_labels}")
    if args.no_cosmos:
        continue
    event = ask_clip(clip)
    print(f"          cosmos: {event.event} / {event.confidence} -> {event.recommended_agent}")
    print(f"          agent:  {handle_event(event)}")
