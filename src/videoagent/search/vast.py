"""Search lane. Talks to the VSS retrieval backend when configured, otherwise a local JSON index.

Both return a list of {clip_id, source, t_start, t_end, description, ...}. Backend hits are
5 s segments; each is registered as a Clip in data/clips/ so ask_clip can load it by clip_id,
and its video is downloaded on first use.
"""

import json
from functools import lru_cache
from pathlib import Path

import requests
import weave

from ..config import settings
from ..schema import Clip


def _local_index() -> list[dict]:
    if not settings.index_path.exists():
        return []
    return json.loads(settings.index_path.read_text())


def _local_search(q: str, k: int) -> list[dict]:
    terms = q.lower().split()
    scored = []
    for item in _local_index():
        text = item.get("description", "").lower()
        score = sum(text.count(t) for t in terms)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [item for _, item in scored[:k]]


def _url(path: str) -> str:
    return settings.vast_base_url.rstrip("/") + path


@lru_cache(maxsize=1)
def _token() -> str:
    resp = requests.post(
        _url("/api/v1/auth/login"),
        json={"username": settings.vast_username, "password": settings.vast_password},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _vss(method: str, path: str, **kwargs) -> requests.Response:
    for attempt in range(2):
        resp = requests.request(
            method, _url(path), headers={"Authorization": f"Bearer {_token()}"}, timeout=120, **kwargs
        )
        if resp.status_code == 401 and attempt == 0:
            _token.cache_clear()
            continue
        resp.raise_for_status()
        return resp
    return resp


def _hit_to_item(hit: dict) -> dict:
    clip_id = Path(hit["filename"]).stem
    item = {
        "clip_id": clip_id,
        "source": hit["source"],
        "original_video": hit.get("original_video"),
        "t_start": float(hit.get("segment_start_sec") or 0),
        "t_end": float(hit.get("segment_end_sec") or 0),
        "description": hit.get("reasoning_content") or "",
        "score": round(float(hit.get("similarity_score") or 0), 3),
        "location": hit.get("location"),
        "camera_id": hit.get("camera_id"),
        "capture_type": hit.get("capture_type"),
        "object_counts": hit.get("object_counts"),
        "uploaded": hit.get("upload_timestamp"),
    }
    clip = Clip(
        clip_id=clip_id,
        source=hit["source"],
        t_start=item["t_start"],
        t_end=item["t_end"],
        path=str(settings.clips_dir / f"{clip_id}.mp4"),
        yolo_labels=[c for c in (hit.get("object_classes") or "").split(",") if c],
    )
    settings.clips_dir.mkdir(parents=True, exist_ok=True)
    (settings.clips_dir / f"{clip_id}.json").write_text(clip.model_dump_json())
    return item


@weave.op()
def search_footage(q: str, k: int = 5, min_similarity: float = 0.2, filters: dict | None = None) -> list[dict]:
    if not settings.vast_base_url:
        return _local_search(q, k)
    body = {"query": q, "top_k": k, "llm_top_n": 1, "min_similarity": min_similarity}
    if filters:
        body["metadata_filters"] = filters
    hits = _vss("POST", "/api/v1/search", json=body).json().get("results", [])
    return [_hit_to_item(h) for h in hits]


def fetch_clip(clip: Clip) -> Clip:
    """Download an archive segment to clip.path if it is not already on disk."""
    path = Path(clip.path)
    if path.exists() or not clip.source.startswith("s3://"):
        return clip
    # The stream endpoint authenticates via a token query parameter, not the Authorization header.
    resp = requests.get(
        _url("/api/v1/videos/stream"), params={"source": clip.source, "token": _token()}, timeout=120
    )
    resp.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return clip


def add_to_local_index(item: dict) -> None:
    index = [i for i in _local_index() if i["clip_id"] != item["clip_id"]]
    index.append(item)
    settings.index_path.parent.mkdir(parents=True, exist_ok=True)
    settings.index_path.write_text(json.dumps(index, indent=2))
