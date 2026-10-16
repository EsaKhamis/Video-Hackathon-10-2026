"""Cosmos Reason 2 over a clip, via an OpenAI-compatible endpoint (e.g. vLLM)."""

import base64
import json
import re

import cv2
import weave
from openai import OpenAI

from ..config import load_prompt, settings
from ..schema import Clip, Event


def sample_frames(path: str, n: int) -> list[bytes]:
    cap = cv2.VideoCapture(path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    idxs = sorted({round(i * (total - 1) / max(n - 1, 1)) for i in range(n)})
    frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if ok:
            frame = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
            frames.append(cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes())
    cap.release()
    return frames


def _parse_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError(f"No JSON in Cosmos output: {text[:200]}")
    return json.loads(match.group(0))


def _complete(prompt: str, clip_path: str, max_tokens: int = 512) -> str:
    if not settings.cosmos_base_url:
        raise RuntimeError("COSMOS_BASE_URL is not set")
    client = OpenAI(base_url=settings.cosmos_base_url, api_key=settings.cosmos_api_key)
    content = [{"type": "text", "text": prompt}] + [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode()},
        }
        for f in sample_frames(clip_path, settings.cosmos_max_frames)
    ]
    resp = client.chat.completions.create(
        model=settings.cosmos_model,
        messages=[{"role": "user", "content": content}],
        temperature=0.2,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content or ""


@weave.op()
def ask_clip(clip: Clip, question: str = "Is anyone missing a hard hat?") -> Event:
    prompt = load_prompt("cosmos").format(
        yolo_labels=", ".join(clip.yolo_labels) or "none", question=question
    )
    data = _parse_json(_complete(prompt, clip.path))
    return Event(clip_id=clip.clip_id, t_start=clip.t_start, t_end=clip.t_end, **data)


@weave.op()
def describe_clip(clip_path: str) -> str:
    text = _complete(load_prompt("ingest"), clip_path, max_tokens=400)
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
