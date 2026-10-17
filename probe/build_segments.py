"""Slim segment index for the app: gopro_all.json (from list_gopro.py) -> data/segments.json.

One row per 5 s segment with its playable S3 `source`, plus the join key screen.json uses
(`clip` = chunk filename, `start_sec`). Kept small so it fits in the app's data ConfigMap.
    python3 probe/build_segments.py [~/bike-probe/gopro_all.json]
"""
import json
import re
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "bike-probe/gopro_all.json")
OUT = Path(__file__).resolve().parent.parent / "data/segments.json"
CHUNK_SEC = 30.0

rows = []
for s in json.loads(SRC.read_text()):
    clip = s["original_video"].rsplit("/", 1)[-1]
    m = re.search(r"_(GOPR\d+|GX\d+)_chunk_(\d+)", clip)
    chunk = int(m.group(2))
    start, end = float(s["segment_start_sec"]), float(s["segment_end_sec"])
    rows.append({
        "ride": m.group(1),
        "clip": clip,
        "chunk_index": chunk,
        "segment_number": int(s["segment_number"]),
        "start_sec": start,
        "end_sec": end,
        "offset_sec": chunk * CHUNK_SEC + start,
        "source": s["source"],
        "camera_id": s.get("camera_id"),
        "description": (s.get("reasoning_content") or "")[:280],
    })
rows.sort(key=lambda r: (r["ride"], r["offset_sec"]))
OUT.write_text(json.dumps(rows, separators=(",", ":")))
print(f"{len(rows)} segments -> {OUT} ({OUT.stat().st_size // 1024} KB)")
