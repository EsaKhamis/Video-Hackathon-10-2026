"""Real-time lane: replay footage at a low fps, track with YOLO11, flag clips.

YOLO runs on every sampled frame. A clip is flagged when a trigger class
persists on a track for TRIGGER_MIN_FRAMES; only flagged clips go to Cosmos.
"""

import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from ..config import settings
from ..schema import Clip


def replay(source: str, fps: float) -> Iterator[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video source: {source}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30
    step = max(int(round(src_fps / fps)), 1)
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            yield i / src_fps, frame
        i += 1
    cap.release()


def write_clip(frames: list[tuple[float, np.ndarray]], clip_id: str, fps: float) -> Path:
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    path = settings.clips_dir / f"{clip_id}.mp4"
    h, w = frames[0][1].shape[:2]
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for _, f in frames:
        out.write(f)
    out.release()
    return path


class Detector:
    def __init__(self) -> None:
        self.model = YOLO(settings.yolo_model)
        self.names = self.model.names
        self.triggers = set(settings.trigger_classes)
        self.seen: dict[int, int] = {}
        self.last_flag: dict[int, float] = {}

    def step(self, t: float, frame: np.ndarray) -> tuple[np.ndarray, list[str], bool]:
        """Returns (annotated frame, labels in frame, whether to flag a clip)."""
        result = self.model.track(frame, persist=True, verbose=False)[0]
        labels, flag = [], False
        boxes = result.boxes
        if boxes is not None and boxes.id is not None:
            for cls, tid in zip(boxes.cls.int().tolist(), boxes.id.int().tolist()):
                name = self.names[cls]
                labels.append(name)
                if name not in self.triggers:
                    continue
                self.seen[tid] = self.seen.get(tid, 0) + 1
                cooled = t - self.last_flag.get(tid, -1e9) >= settings.trigger_cooldown_s
                if self.seen[tid] >= settings.trigger_min_frames and cooled:
                    self.last_flag[tid] = t
                    flag = True
        return result.plot(), sorted(set(labels)), flag


def run(
    source: str | None = None, realtime: bool = True
) -> Iterator[tuple[float, np.ndarray, Clip | None]]:
    """Yields (t, annotated frame, flagged clip or None) for each sampled frame."""
    source = source or settings.video_source
    fps = settings.stream_fps
    buffer: deque[tuple[float, np.ndarray]] = deque(maxlen=max(int(fps * settings.clip_seconds), 2))
    detector = Detector()
    stem = Path(source).stem

    for t, frame in replay(source, fps):
        tick = time.time()
        buffer.append((t, frame))
        annotated, labels, flag = detector.step(t, frame)
        clip = None
        if flag and len(buffer) >= 2:
            frames = list(buffer)
            clip_id = f"{stem}_{int(t * 10):05d}"
            path = write_clip(frames, clip_id, fps)
            clip = Clip(
                clip_id=clip_id,
                source=source,
                t_start=frames[0][0],
                t_end=frames[-1][0],
                path=str(path),
                yolo_labels=labels,
            )
            path.with_suffix(".json").write_text(clip.model_dump_json())
        yield t, annotated, clip
        if realtime:
            time.sleep(max(0.0, 1 / fps - (time.time() - tick)))
