"""Search lane. Talks to VAST when configured, otherwise a local JSON index.

The local index is a list of {clip_id, source, t_start, t_end, description}
so the agent and UI can be built before VAST access is sorted out.
"""

import json

import requests
import weave

from ..config import settings


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


@weave.op()
def search_footage(q: str, k: int = 5) -> list[dict]:
    if not settings.vast_base_url:
        return _local_search(q, k)

    # TODO(build day): match the real VAST search endpoint and response shape.
    resp = requests.post(
        f"{settings.vast_base_url.rstrip('/')}/search",
        headers={"Authorization": f"Bearer {settings.vast_api_key}"},
        json={"query": q, "top_k": k},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def add_to_local_index(item: dict) -> None:
    index = [i for i in _local_index() if i["clip_id"] != item["clip_id"]]
    index.append(item)
    settings.index_path.parent.mkdir(parents=True, exist_ok=True)
    settings.index_path.write_text(json.dumps(index, indent=2))
