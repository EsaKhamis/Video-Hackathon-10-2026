"""Search lane ingest: cut footage into clips, describe each with Cosmos + the
ingest prompt, and add to the local index. On build day VAST does this step
with our prompt; use this to test search before VAST is wired up.

    uv run scripts/ingest.py data/footage/*.mp4 [--clip-seconds 8]
"""

import argparse
from pathlib import Path

import cv2

from videoagent import tracing
from videoagent.config import settings
from videoagent.reason.cosmos import describe_clip
from videoagent.schema import Clip
from videoagent.search.vast import add_to_local_index


def split(source: Path, seconds: float):
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    per_clip = int(fps * seconds)
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        clip_id = f"{source.stem}_{n:04d}"
        path = settings.clips_dir / f"{clip_id}.mp4"
        out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        frames = 0
        for _ in range(per_clip):
            ok, frame = cap.read()
            if not ok:
                break
            out.write(frame)
            frames += 1
        out.release()
        if frames == 0:
            path.unlink(missing_ok=True)
            break
        yield Clip(
            clip_id=clip_id,
            source=str(source),
            t_start=n * seconds,
            t_end=n * seconds + frames / fps,
            path=str(path),
        )
        n += 1
    cap.release()


parser = argparse.ArgumentParser()
parser.add_argument("videos", nargs="+", type=Path)
parser.add_argument("--clip-seconds", type=float, default=8)
args = parser.parse_args()

tracing.init()
for video in args.videos:
    for clip in split(video, args.clip_seconds):
        desc = describe_clip(clip.path)
        Path(clip.path).with_suffix(".json").write_text(clip.model_dump_json())
        add_to_local_index({**clip.model_dump(), "description": desc})
        print(f"{clip.clip_id} [{clip.t_start:.0f}-{clip.t_end:.0f}s] {desc[:80]!r}")
