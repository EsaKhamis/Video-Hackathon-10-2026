"""Real-time lane: replay footage at a low fps, track with YOLO11, flag clips.

YOLO runs on every sampled frame. In "yolo" mode a clip is flagged when a
trigger class persists on a track for TRIGGER_MIN_FRAMES; in "scoreboard" mode
when the score overlay changes. Only flagged clips go to Cosmos.
"""

import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

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


class ScoreboardTrigger:
    """Flags when the score overlay changes and then holds still, i.e. a point ended.

    Counts pixels that changed sharply rather than mean difference, so a single
    digit flip registers but video compression noise does not.
    """

    PIXEL_DELTA = 40
    MIN_CHANGED = 40
    MAX_JITTER = 10

    def __init__(self, roi: tuple[int, ...], stable_frames: int) -> None:
        self.x1, self.y1, self.x2, self.y2 = roi
        self.stable_frames = stable_frames
        self.ref: np.ndarray | None = None
        self.prev: np.ndarray | None = None
        self.stable = 0

    def _changed(self, a: np.ndarray, b: np.ndarray) -> int:
        return int((np.abs(a - b) > self.PIXEL_DELTA).sum())

    def step(self, frame: np.ndarray) -> bool:
        crop = frame[self.y1 : self.y2, self.x1 : self.x2]
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.int16)
        prev, self.prev = self.prev, g
        if prev is None:
            return False
        self.stable = self.stable + 1 if self._changed(g, prev) < self.MAX_JITTER else 0
        if self.stable < self.stable_frames:
            return False
        if self.ref is None:
            self.ref = g
            return False
        if self._changed(g, self.ref) > self.MIN_CHANGED:
            self.ref = g
            return True
        return False


class Detector:
    def __init__(self) -> None:
        from ultralytics import YOLO  # optional extra: uv sync --extra realtime

        self.model = YOLO(settings.yolo_model)
        self.names = self.model.names
        self.triggers = set(settings.trigger_classes)
        self.seen: dict[int, int] = {}
        self.last_flag: dict[int, float] = {}
        self.scoreboard = (
            ScoreboardTrigger(settings.scoreboard_roi, stable_frames=max(2, round(settings.stream_fps)))
            if settings.trigger_mode == "scoreboard"
            else None
        )

    def step(self, t: float, frame: np.ndarray) -> tuple[np.ndarray, list[str], bool]:
        """Returns (annotated frame, labels in frame, whether to flag a clip)."""
        result = self.model.track(frame, persist=True, verbose=False)[0]
        labels, flag = [], False
        if self.scoreboard is not None:
            flag = self.scoreboard.step(frame)
        boxes = result.boxes
        if boxes is not None and boxes.id is not None:
            for cls, tid in zip(boxes.cls.int().tolist(), boxes.id.int().tolist()):
                name = self.names[cls]
                labels.append(name)
                if self.scoreboard is not None or name not in self.triggers:
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
