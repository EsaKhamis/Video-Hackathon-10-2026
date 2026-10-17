#!/usr/bin/env python3
"""Helmet-cam witness app (stdlib only).

Reads app/incidents.json when present, else incidents.stub.json.
Proxies /api/stream (Range → 206) and /api/detections; JWT stays server-side.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STUB = ROOT / "incidents.stub.json"
SEGMENTS_FILE = ROOT / "segments.json"
PORT = int(os.environ.get("PORT", "8090"))
# In the pod the code is a read-only ConfigMap mount: reviews go to a writable path, seeded from app/reviews.json.
REVIEWS = Path(os.environ.get("REVIEWS_PATH", str(ROOT / "reviews.json")))
DEVICES = Path(os.environ.get("DEVICES_PATH", str(ROOT / "devices.json")))
# Engine output lives in its own ConfigMap at /data (k8s/update-data.sh), re-read on every request.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
if not DATA_DIR.is_dir():
    DATA_DIR = ROOT.parent / "data"
SEGMENT_KEY = re.compile(r"((?:GOPR|GX)\d+_chunk_\d+_segment_\d+)")
# Public URL prefix when behind Ingress path /app (rewrite strips it before the app sees the path).
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
VASTDB_TABLE = os.environ.get("VASTDB_INCIDENTS_TABLE", "witness-incidents")


def u(path: str = "/") -> str:
    """Client-facing path, prefixed with BASE_PATH when deployed at /app."""
    if not path.startswith("/"):
        path = "/" + path
    return f"{BASE_PATH}{path}" if BASE_PATH else path


ART = {
    "road": ("art-road.jpg", "image/jpeg"),
    "tree": ("art-tree.jpg", "image/jpeg"),
    "tree-green": ("art-tree-green.jpg", "image/jpeg"),
    "tree-flowers": ("art-tree-flowers.jpg", "image/jpeg"),
    "bike": ("art-bike.jpg", "image/jpeg"),
    "street": ("art-street.jpg", "image/jpeg"),
    "cars": ("art-cars.jpg", "image/jpeg"),
    "signal-rider": ("art-signal-rider.jpg", "image/jpeg"),
    "signal": ("art-signal.jpg", "image/jpeg"),
    "taxi": ("art-taxi.jpg", "image/jpeg"),
    "police": ("art-police.jpg", "image/jpeg"),
    "safe": ("art-safe.jpg", "image/jpeg"),
    "flower": ("art-flower.jpg", "image/jpeg"),
    "meadow": ("art-meadow.jpg", "image/jpeg"),
    "ground": ("art-ground.png", "image/png"),
    "license": ("art-license.png", "image/png"),
    "bike-icon": ("art-bike-icon.jpg", "image/jpeg"),
    "walker": ("art-walker.jpg", "image/jpeg"),
}
RIDE_ART = ("street", "bike", "cars", "signal-rider")


def art(key: str, alt: str, cls: str = "illu") -> str:
    if key not in ART:
        return ""
    return f'<img class="{esc(cls)}" src="{u("/static/" + key)}" alt="{esc(alt)}">'


def scene_for_fault(fault: str, item: dict | None = None) -> tuple[str, str]:
    facts = (item or {}).get("facts") or {}
    if fault == "rider" and facts.get("signal_for_rider") == "red":
        return "signal-rider", "Rider waiting at a red signal"
    if fault == "rider":
        return "bike", "Rider on a bicycle"
    if fault == "driver":
        return "taxi", "Driver in a close call"
    if fault == "shared":
        return "street", "A car beside a rider"
    return "street", "A car beside a rider"


def strip_base(path: str) -> str:
    if BASE_PATH and (path == BASE_PATH or path.startswith(BASE_PATH + "/")):
        return path[len(BASE_PATH) :] or "/"
    return path


# #region agent log
_DEBUG_LOG = Path("/home/ria22spberry/.cursor/debug-e63ac1.log")


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        payload = {
            "sessionId": "e63ac1",
            "runId": "basepath-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(__import__("time").time() * 1000),
        }
        with _DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass


# #endregion

FAULT_LABEL = {
    "driver": "Driver",
    "rider": "Rider",
    "unclear": "Unclear",
    "shared": "Shared",
}

PASS_THROUGH_HEADERS = (
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
    "Cache-Control",
)

_token = None
_token_lock = threading.Lock()
_cfg_lock = threading.Lock()
_cfg: dict[str, str] | None = None


def load_cfg() -> dict[str, str]:
    global _cfg
    with _cfg_lock:
        if _cfg is not None:
            return _cfg
        cfg: dict[str, str] = {}
        for key in ("VSS_URL", "INGRESS_URL", "VSS_USERNAME", "USERNAME", "VSS_PASSWORD", "PASSWORD"):
            if os.environ.get(key):
                cfg[key] = os.environ[key]
        config_files = sorted(Path("/config").glob("*.config")) if Path("/config").is_dir() else []
        if len(config_files) == 1:
            for line in config_files[0].read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    cfg[k] = v.strip().strip('"').strip("'")
        backend = (cfg.get("VSS_URL") or cfg.get("INGRESS_URL") or "").rstrip("/")
        user = cfg.get("VSS_USERNAME") or cfg.get("USERNAME") or ""
        password = cfg.get("VSS_PASSWORD") or cfg.get("PASSWORD") or ""
        _cfg = {"backend": backend, "username": user, "password": password}
        return _cfg


def login() -> str:
    cfg = load_cfg()
    body = json.dumps({"username": cfg["username"], "password": cfg["password"]}).encode()
    req = urllib.request.Request(
        cfg["backend"] + "/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def get_token(refresh: bool = False) -> str:
    global _token
    with _token_lock:
        if refresh or _token is None:
            _token = login()
        return _token


def vss_open(path: str, params: dict, headers: dict | None = None, timeout: int = 60):
    cfg = load_cfg()
    for attempt in (0, 1):
        q = urllib.parse.urlencode({**params, "token": get_token(refresh=attempt == 1)})
        req = urllib.request.Request(f"{cfg['backend']}{path}?{q}", headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0:
                continue
            raise


def vss_get_json(path: str, params: dict | None = None) -> dict:
    cfg = load_cfg()
    for attempt in (0, 1):
        headers = {"Authorization": f"Bearer {get_token(refresh=attempt == 1)}"}
        url = cfg["backend"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0:
                continue
            raise


def load_reviews() -> dict:
    for path in (REVIEWS, ROOT / "reviews.json"):
        if path.exists():
            return json.loads(path.read_text())
    return {}


def save_reviews(reviews: dict) -> None:
    REVIEWS.write_text(json.dumps(reviews, indent=2) + "\n")


def read_data(name: str, default):
    try:
        return json.loads((DATA_DIR / name).read_text())
    except FileNotFoundError:
        return default


def resolve_source(source: str) -> str:
    """'GOPR0130_chunk_0026_segment_003' -> playable s3:// segment URI from data/segments.json."""
    if not source or source.startswith("s3://"):
        return source or ""
    m = SEGMENT_KEY.search(source)
    if not m:
        return ""
    for s in read_data("segments.json", []):
        if m.group(1) in s["source"]:
            return s["source"]
    return ""


def load_rides():
    """Segments (playable sources) joined with Stage A screening rows on (chunk file, start second)."""
    screen = read_data("screen.json", {})
    by_key = {(r["clip"], float(r["start_sec"])): r for r in screen.get("rows", [])}
    rides: dict[str, list] = {}
    for s in read_data("segments.json", []):
        r = by_key.get((s["clip"], s["start_sec"]))
        seg = dict(s, screened=r is not None)
        if r:
            seg.update(screen=r.get("screen"), label=r.get("label"), fact=r.get("fact"),
                       red=r.get("red"), screen_id=r.get("id"))
        rides.setdefault(s["ride"], []).append(seg)
    return rides, screen


def ride_summaries(rides, screen) -> dict:
    out = []
    for ride, segs in sorted(rides.items()):
        found: dict[str, int] = {}
        for s in segs:
            if s["screened"]:
                found[s["screen"]] = found.get(s["screen"], 0) + 1
        out.append({
            "ride": ride,
            "segments": len(segs),
            "screened": sum(s["screened"] for s in segs),
            "duration_sec": max(s["offset_sec"] + s["end_sec"] - s["start_sec"] for s in segs),
            "counts": found,
        })
    return {"rides": out, "legend": screen.get("legend", {}), "stage": screen.get("stage"),
            "partial": screen.get("partial")}


def load_devices() -> dict:
    if not DEVICES.exists():
        return {"registered": []}
    try:
        data = json.loads(DEVICES.read_text())
    except json.JSONDecodeError:
        return {"registered": []}
    if not isinstance(data.get("registered"), list):
        data["registered"] = []
    return data


def save_devices(devices: dict) -> None:
    DEVICES.write_text(json.dumps(devices, indent=2) + "\n")


def camera_id_for_ride(ride: dict) -> str:
    if ride.get("camera_id"):
        return str(ride["camera_id"])
    rid = ride.get("id")
    for seg in load_segments():
        if seg.get("ride") == rid and seg.get("camera_id"):
            return str(seg["camera_id"])
    return "unknown"


def cameras_in_data(data: dict) -> list[str]:
    found: list[str] = []
    for ride in data.get("rides") or []:
        cam = camera_id_for_ride(ride)
        if cam not in found:
            found.append(cam)
    for seg in load_segments():
        cam = seg.get("camera_id")
        if cam and cam not in found:
            found.append(str(cam))
    return found


_segments_cache: list[dict] | None = None
_segments_key: tuple[str, float] | None = None
_data_source = "stub"


def data_source() -> str:
    return _data_source


def _team_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for key in (
        "S3_ENDPOINT",
        "ACCESS_KEY",
        "SECRET_KEY",
        "VASTDB_BUCKET",
        "VDB_SCHEMA",
        "VDB_ENDPOINT",
    ):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    candidates = [Path("/config/team.config")]
    if Path("/config").is_dir():
        candidates.extend(sorted(Path("/config").glob("*.config")))
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                cfg.setdefault(k, v.strip().strip('"').strip("'"))
        break
    return cfg


def load_from_vastdb() -> dict | None:
    """Optional: read D's witness-incidents table when vastdb is installed (K8s pod)."""
    try:
        import vastdb  # type: ignore
    except ImportError:
        return None
    cfg = _team_config()
    endpoint = (cfg.get("VDB_ENDPOINT") or cfg.get("S3_ENDPOINT") or "").rstrip("/")
    if endpoint and not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    bucket = cfg.get("VASTDB_BUCKET") or ""
    schema = cfg.get("VDB_SCHEMA") or "vss-schema"
    access = cfg.get("ACCESS_KEY") or ""
    secret = cfg.get("SECRET_KEY") or ""
    if not (endpoint and bucket and access and secret):
        return None
    try:
        session = vastdb.connect(endpoint=endpoint, access=access, secret=secret, ssl_verify=False)
        with session.transaction() as tx:
            table = tx.bucket(bucket).schema(schema).table(VASTDB_TABLE)
            cols = [c.name for c in table.columns()]
            prefer = [c for c in ("payload", "document", "json", "row_json", "data") if c in cols]
            if prefer:
                arrow = table.select(columns=prefer).read_all()
                rows = arrow.to_pylist()
                incidents = []
                for row in rows:
                    raw = row.get(prefer[0])
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode()
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    if isinstance(raw, dict) and raw.get("id"):
                        incidents.append(raw)
                    elif isinstance(raw, dict) and raw.get("incidents"):
                        return raw
                if not incidents:
                    return None
                rides = {}
                for item in incidents:
                    rid = item.get("ride") or "unknown"
                    rides.setdefault(rid, {"id": rid, "title": rid})
                return {
                    "generated_from": f"VastDB {schema}.{VASTDB_TABLE}",
                    "rides": list(rides.values()),
                    "incidents": incidents,
                }
            # Fallback: whole-row JSON columns unknown — skip rather than guess.
            return None
    except Exception as exc:
        # #region agent log
        _dbg("H7", "load_from_vastdb", "failed", {"error": type(exc).__name__})
        # #endregion
        return None


def load_segments() -> list[dict]:
    global _segments_cache, _segments_key
    for path in (DATA_DIR / "segments.json", SEGMENTS_FILE):
        if path.is_file():
            key = (str(path), path.stat().st_mtime)
            if _segments_cache is None or _segments_key != key:
                _segments_cache = json.loads(path.read_text())
                _segments_key = key
            return _segments_cache
    return []


def find_segment(source: str) -> dict | None:
    if not source:
        return None
    for seg in load_segments():
        if seg.get("source") == source:
            return seg
    # Match by filename tail when URI prefixes differ slightly.
    needle = source.rsplit("/", 1)[-1]
    for seg in load_segments():
        if (seg.get("source") or "").rsplit("/", 1)[-1] == needle:
            return seg
    return None


def segment_neighbors(source: str) -> tuple[dict | None, dict | None, dict | None]:
    """Lead-up / peak / aftermath by ride order (chunk_index, segment_number) — never invent filenames."""
    peak = find_segment(source)
    if peak is None:
        return None, None, None
    group = [s for s in load_segments() if s.get("ride") == peak.get("ride")]
    group.sort(
        key=lambda s: (
            s.get("offset_sec") if s.get("offset_sec") is not None else 10**9,
            s.get("chunk_index") or 0,
            s.get("segment_number") or 0,
        )
    )
    try:
        idx = next(i for i, s in enumerate(group) if s.get("source") == peak.get("source"))
    except StopIteration:
        return None, peak, None
    prev_seg = group[idx - 1] if idx > 0 else None
    next_seg = group[idx + 1] if idx + 1 < len(group) else None
    return prev_seg, peak, next_seg


def load_data() -> dict:
    global _data_source
    data = None
    for path in (DATA_DIR / "incidents.json", ROOT / "incidents.json"):
        if path.is_file():
            data = json.loads(path.read_text())
            _data_source = str(path)
            break
    if data is None:
        data = load_from_vastdb()
        if data is not None:
            _data_source = f"vastdb:{VASTDB_TABLE}"
    if data is None:
        data = json.loads(STUB.read_text())
        _data_source = "stub"
    reviews = load_reviews()
    for item in data.get("incidents") or []:
        if not item.get("preview_source"):
            item["preview_source"] = resolve_source(item.get("source", ""))
        vehicle = (item.get("facts") or {}).get("vehicle") or {}
        if (vehicle.get("colour") or "").strip().lower() in ("cyclist to confirm", "unknown", "none"):
            vehicle["colour"] = ""
        override = reviews.get(item["id"])
        if override:
            item["review"] = {**item.get("review", {}), **override}
            if override.get("fault_override"):
                item["fault"] = override["fault_override"]
    for ride in data.get("rides") or []:
        if not ride.get("camera_id"):
            ride["camera_id"] = camera_id_for_ride(ride)
    # #region agent log
    _dbg(
        "H7",
        "load_data",
        "source",
        {
            "source": _data_source,
            "incidents": len(data.get("incidents") or []),
            "segments": len(load_segments()),
            "cameras": cameras_in_data(data),
        },
    )
    # #endregion
    return data


def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def counts(incidents: list[dict]) -> dict[str, int]:
    out = {"driver": 0, "rider": 0, "unclear": 0}
    for item in incidents:
        fault = item.get("fault")
        if fault in out:
            out[fault] += 1
    return out


def summary_line(found: dict[str, int]) -> str:
    return (
        f"{found['driver']} driver incident{'s' if found['driver'] != 1 else ''}, "
        f"{found['rider']} rider lesson{'s' if found['rider'] != 1 else ''}"
    )


def page(title: str, body: str, extra_head: str = "", *, show_trees: bool = True) -> bytes:
    left_tree = art("tree", "Yellow tree", "tree") if show_trees else ""
    right_tree = art("tree-green", "Green tree", "tree") if show_trees else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Witness</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    color-scheme: light;
    --ink: #2a3b4f;
    --muted: #4d6270;
    --line: #2a3b4f;
    --paper: #d7f0ff;
    --card: #fffdf6;
    --driver: #d23b2f;
    --rider: #e39b12;
    --unclear: #3d6b8a;
    --lane: #7dce6a;
    --sky: #8ecdf2;
    --grass: #8ed36a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    color: var(--ink);
    font: 16px/1.5 Sora, "Segoe UI", sans-serif;
    background: linear-gradient(180deg, var(--sky) 0 220px, #e7f8c8 220px 420px, var(--grass) 420px);
  }}
  a {{ color: inherit; }}
  .shell {{
    position: relative;
    width: min(980px, calc(100% - 32px));
    margin: 0 auto;
    padding: 28px 0 48px;
  }}
  .mast {{
    display: flex;
    align-items: flex-end;
    gap: 10px;
    margin: 0 0 14px;
  }}
  .mast .tree {{
    width: 72px;
    height: 96px;
    object-fit: contain;
    background: transparent;
  }}
  .brand {{
    font-family: Fraunces, Georgia, serif;
    font-size: clamp(2.6rem, 7vw, 4.2rem);
    font-weight: 700;
    letter-spacing: -0.04em;
    line-height: 0.95;
    margin: 0;
    animation: rise 0.7s ease both;
  }}
  .brand span {{
    display: inline-block;
    color: transparent;
    background: linear-gradient(120deg, var(--ink) 20%, #2f6b3a 55%, var(--rider) 100%);
    -webkit-background-clip: text;
    background-clip: text;
  }}
  .shell > *:not(.brand) {{ animation: rise 0.65s ease both; animation-delay: 0.08s; }}
  @keyframes rise {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to {{ opacity: 1; transform: none; }}
  }}
  .meta, header p {{ color: var(--muted); }}
  h1 {{
    font-family: Fraunces, Georgia, serif;
    font-size: clamp(1.7rem, 3.5vw, 2.25rem);
    font-weight: 700;
    margin: 0 0 8px;
    letter-spacing: -0.03em;
  }}
  h2 {{
    font-family: Fraunces, Georgia, serif;
    font-size: 1.25rem;
    font-weight: 600;
    margin: 0;
  }}
  h3 {{
    font: 600 12px/1.2 Sora, sans-serif;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 10px;
  }}
  .kicker {{
    font: 600 12px/1.2 Sora, sans-serif;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 10px;
  }}
  .kicker a {{ text-decoration: none; border-bottom: 1px solid transparent; }}
  .kicker a:hover {{ border-color: var(--ink); }}
  .rides, .list {{ display: grid; gap: 14px; margin: 22px 0 36px; }}
  .card {{
    display: block;
    background: var(--card);
    border: 3px solid var(--line);
    border-radius: 18px;
    padding: 16px 18px;
    text-decoration: none;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
  }}
  .card.with-art {{
    display: grid;
    grid-template-columns: 96px 1fr;
    gap: 14px;
    align-items: center;
  }}
  .card .thumb {{
    width: 96px;
    height: 96px;
    object-fit: contain;
  }}
  .card:hover {{
    transform: translateY(-3px);
    box-shadow: 6px 8px 0 rgba(42, 59, 79, 0.18);
  }}
  .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
  .summary {{
    font-family: Fraunces, Georgia, serif;
    font-size: clamp(1.15rem, 2.4vw, 1.45rem);
    margin: 8px 0 0;
    letter-spacing: -0.02em;
  }}
  .tabs {{ display: flex; gap: 8px; margin: 18px 0; flex-wrap: wrap; }}
  .tabs a, .tabs button {{
    font: 500 14px/1 Sora, sans-serif;
    text-decoration: none;
    border: 3px solid var(--line);
    border-radius: 999px;
    padding: 10px 14px;
    background: #fffdf6;
    color: inherit;
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
  }}
  .tabs a.on, .tabs button.on {{ background: var(--ink); color: #f4f7f4; border-color: var(--ink); }}
  .badge {{
    font: 600 11px/1 Sora, sans-serif;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 5px 8px;
    color: white;
  }}
  .badge.driver {{ background: var(--driver); }}
  .badge.rider {{ background: var(--rider); }}
  .badge.unclear, .badge.shared {{ background: var(--unclear); }}
  .when {{ font: 13px/1.3 Sora, sans-serif; color: var(--muted); }}
  .empty {{ color: var(--muted); }}
  .stage {{ display: grid; gap: 16px; margin: 18px 0 40px; }}
  @media (min-width: 860px) {{
    .stage {{ grid-template-columns: 1.4fr 1fr; align-items: start; }}
  }}
  .panel {{
    background: var(--card);
    border: 3px solid var(--line);
    border-radius: 18px;
    padding: 18px;
  }}
  .banner {{
    border: 3px solid var(--line);
    border-radius: 18px;
    overflow: hidden;
    margin: 0 0 16px;
    background: #5f6b73;
  }}
  .banner img {{ display: block; width: 100%; height: auto; object-fit: fill; }}
  .split {{
    display: grid;
    grid-template-columns: 120px 1fr;
    gap: 14px;
    align-items: center;
  }}
  .split img, .figure {{
    width: 120px;
    height: 120px;
    object-fit: contain;
  }}
  .flower-strip {{
    display: block;
    width: 100%;
    height: auto;
    object-fit: fill;
    border: 3px solid var(--line);
    border-radius: 18px;
    margin: 8px 0 18px;
  }}
  @media (max-width: 640px) {{
    .card.with-art, .split {{ grid-template-columns: 72px 1fr; }}
    .card .thumb, .split img, .figure {{ width: 72px; height: 72px; }}
    .mast .tree {{ width: 48px; height: 64px; }}
  }}
  .player {{
    position: relative;
    background: #0d1410;
    aspect-ratio: 16/9;
    overflow: hidden;
  }}
  .player video, .player canvas {{
    position: absolute; inset: 0; width: 100%; height: 100%;
  }}
  .player canvas {{ pointer-events: none; }}
  .story {{ margin: 0 0 14px; font-size: 17px; line-height: 1.55; }}
  details.tech {{ margin-top: 4px; }}
  details.tech summary {{
    font: 600 12px/1.2 Sora, sans-serif;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    cursor: pointer;
  }}
  .facts {{ display: grid; gap: 8px; font: 14px/1.4 Sora, sans-serif; margin-top: 10px; }}
  .facts div {{ display: grid; grid-template-columns: 9.5rem 1fr; gap: 8px; }}
  .facts dt {{ color: var(--muted); }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
  .actions button, .actions a.button {{
    font: 500 14px/1 Sora, sans-serif;
    border: 1px solid var(--ink);
    background: transparent;
    color: var(--ink);
    padding: 11px 14px;
    cursor: pointer;
    text-decoration: none;
  }}
  .actions button.on, .actions a.button.on {{
    background: var(--ink);
    color: #f4f7f4;
  }}
  .actions button.secondary, .actions a.button.secondary {{
    background: transparent;
    color: var(--ink);
  }}
  .hint {{ font: 13px/1.45 Sora, sans-serif; color: var(--muted); margin: 0 0 10px; }}
  .audience {{
    font: 15px/1.5 Sora, sans-serif;
    margin: 0 0 14px;
    padding: 12px 14px;
    border-left: 3px solid var(--lane);
    background: rgba(255,255,255,0.5);
  }}
  label {{ display: grid; gap: 4px; font: 500 13px/1.3 Sora, sans-serif; margin-top: 10px; }}
  input, select {{
    font: 14px/1.3 Sora, sans-serif;
    padding: 10px 12px;
    border: 1px solid var(--line);
    background: rgba(255,255,255,0.9);
  }}
  .status {{ font: 13px/1.3 Sora, sans-serif; color: var(--muted); margin-top: 10px; }}
  .agree {{ font: 13px/1.3 Sora, sans-serif; margin-top: 8px; }}
  .agree.yes {{ color: #2f6b3a; }}
  .agree.no {{ color: var(--driver); }}
  .timeline {{ position: relative; height: 32px; margin: 18px 0 8px; }}
  .timeline .track {{
    position: absolute; left: 0; right: 0; top: 14px; height: 3px;
    background: linear-gradient(90deg, #c9d4cc, #9eb5a4);
  }}
  .timeline .mark {{
    position: absolute; top: 6px; width: 18px; height: 18px; margin-left: -9px;
    border-radius: 50%; border: 2px solid #f4f7f4;
    animation: pulse 1.8s ease-in-out infinite;
  }}
  .timeline .mark.driver {{ background: var(--driver); }}
  .timeline .mark.rider {{ background: var(--rider); }}
  .timeline .mark.unclear, .timeline .mark.shared {{ background: var(--unclear); }}
  @keyframes pulse {{
    0%, 100% {{ transform: scale(1); }}
    50% {{ transform: scale(1.12); }}
  }}
  .navrow {{ display: flex; justify-content: space-between; gap: 12px; margin: 12px 0 0; font: 13px/1.3 Sora, sans-serif; }}
  .segbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 0; }}
  .segbar button {{
    font: 500 13px/1 Sora, sans-serif;
    border: 1px solid var(--line); background: rgba(255,255,255,0.55); color: var(--ink);
    padding: 9px 12px; cursor: pointer;
  }}
  .segbar button.on {{ border-color: var(--ink); background: var(--ink); color: #f4f7f4; }}
  .segbar button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
  .lesson-inline {{ margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }}
  .lesson-inline ul, .reminders {{ margin: 8px 0 0; padding-left: 1.2rem; }}
  .lesson-inline li, .reminders li {{ margin: 4px 0; }}
  .foot {{
    margin: 0 0 12px;
    font: 13px/1.4 Sora, sans-serif; color: var(--muted);
  }}
  .foot a {{ text-decoration: none; border-bottom: 1px solid var(--line); }}
  .foot a:hover {{ border-color: var(--ink); }}
  .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 14px 0 6px; }}
  .toolbar .button {{
    font: 500 14px/1 Sora, sans-serif;
    border: 1px solid var(--ink);
    background: var(--ink);
    color: #f4f7f4;
    padding: 11px 14px;
    text-decoration: none;
    display: inline-block;
  }}
  .toolbar .button.secondary {{
    background: rgba(255,255,255,0.55);
    color: var(--ink);
  }}
  .cam {{
    font: 500 12px/1.3 Sora, sans-serif;
    color: #3d5248;
    letter-spacing: 0.01em;
  }}
  .device-list {{ list-style: none; padding: 0; margin: 12px 0 0; display: grid; gap: 8px; }}
  .device-list li {{
    background: var(--card);
    border: 1px solid var(--line);
    padding: 12px 14px;
    font: 14px/1.4 Sora, sans-serif;
  }}
  .device-list code {{ font-size: 13px; }}
  #page-audience {{ max-width: 36rem; }}
  @media (prefers-reduced-motion: reduce) {{
    .brand, .shell > *:not(.brand), .timeline .mark, .card {{ animation: none; transition: none; }}
  }}
</style>
{extra_head}
</head>
<body>
<div class="shell">
  <div class="mast">
    {left_tree}
    <p class="brand"><span>Witness</span></p>
    {right_tree}
  </div>
  {body}
</div>
</body>
</html>"""
    return html.encode()


def ride_label(ride: dict) -> str:
    return ride.get("title") or ride.get("id") or "Ride"


def accuracy_html(ev: dict | None) -> str:
    if not ev or not ev.get("total"):
        return ""
    reviewed = f' ({esc(ev["reviewed"])} labelled by your reviews here)' if ev.get("reviewed") else ""
    return (f'<p><strong>On clips we checked by eye{reviewed}:</strong> the rules agree with the label on '
            f'{esc(ev["agree"])} of {esc(ev["total"])}, including {esc(ev.get("heroes_ok"))} of {esc(ev.get("heroes"))} '
            f'hero clips, and gave <strong>{esc(ev["wrong_driver"])}</strong> driver verdicts to clips where the driver '
            f'was not at fault (rules {esc(ev.get("rules"))}). A small set checked by eye, not a benchmark.</p>')


def written_by_line(incidents: list[dict]) -> str:
    models = [(row.get("document") or {}).get("generated_by") for row in incidents]
    written = [m for m in models if m]
    if not written:
        return "Report and lesson text: built from the facts with fixed templates."
    rest = "" if len(written) == len(incidents) else "; the rest use fixed templates"
    return (f"Report and lesson text: {len(written)} of {len(incidents)} written by {written[0]} "
            f"from the facts above{rest}.")


def about_page(data: dict) -> bytes:
    found = counts(data.get("incidents") or [])
    body = f"""<p class="kicker"><a href="{u('/')}">Your rides</a></p>
<h1>About / how it works</h1>
<div class="split" style="margin:12px 0 16px;">
  {art("bike", "Cyclist in a helmet")}
  <p class="summary" style="margin:0;">A helmet-cam witness that drafts a report when a driver is at fault, and a private lesson when you are.</p>
</div>
<section class="panel" style="margin:18px 0;">
  <h3>Pipeline</h3>
  <p>Cosmos3-Reason screens every clip and re-checks each candidate five times; YOLO measures how close each vehicle got; plain rules decide fault. Report and lesson text are built from those facts. The app only reads the result and lets you review it.</p>
  {accuracy_html(data.get("evaluation"))}
  <p class="meta">Data source now: <code>{esc(data_source())}</code> · {esc(len(data.get('incidents') or []))} incidents · {esc(len(load_segments()))} segments indexed for lead-up/aftermath.</p>
  <p class="meta">Ride safety on this load: {esc(summary_line(found))}.</p>
  <p class="meta">{esc(written_by_line(data.get('incidents') or []))}</p>
</section>
<section class="panel">
  <h3>What we do not do</h3>
  <p>Nothing is sent to police or 311 automatically. Lessons never claim a fine or offence. Adjacent-lane traffic is filtered on purpose.</p>
</section>"""
    return page("About", body)


def devices_page(data: dict) -> bytes:
    registered = load_devices().get("registered") or []
    known = cameras_in_data(data)
    known_rows = "".join(
        f"<li><code>{esc(cam)}</code> — on your indexed rides"
        + (" · <b>registered</b>" if cam in registered else "")
        + "</li>"
        for cam in known
    ) or "<li>No camera ids in the current load yet.</li>"
    reg_rows = "".join(f"<li><code>{esc(cam)}</code></li>" for cam in registered) or (
        "<li class='empty'>None yet — add your helmet cam or dashcam id below.</li>"
    )
    examples = "".join(
        f"<li><code>{esc(cam)}</code></li>"
        for cam in ("nyc_bike_gopro-1", "pie_cam-3", "team-dashcam-a")
        if cam not in known
    )
    body = f"""<p class="kicker"><a href="{u('/')}">Your rides</a></p>
<div class="split">
  {art("license", "Illustrated bicycle registration card")}
  <h1>Register device</h1>
</div>
<p id="page-audience">Each camera in the archive has a <b>camera id</b> (the stream label VSS indexed). That is why you see many ids in the lab — every helmet cam, dashcam, and team feed gets its own. Your rides show their camera id in brackets so you know which device filmed them.</p>
<section class="panel" style="margin:18px 0;">
  <h3>On your rides now</h3>
  <ul class="device-list">{known_rows}</ul>
  {"<p class='hint' style='margin-top:12px'>Other ids you may see in the wider archive (not on this stub load):</p><ul class='device-list'>" + examples + "</ul>" if examples else ""}
</section>
<section class="panel">
  <h3>Your registered devices</h3>
  <ul class="device-list" id="reg-list">{reg_rows}</ul>
  <label>Camera id
    <span class="hint">Paste the stream id for your camera (e.g. nyc_bike_gopro-1). Demo only — stored in this app, not sent to police.</span>
    <input id="camera-id" placeholder="e.g. nyc_bike_gopro-1" value="">
  </label>
  <div class="actions">
    <button type="button" id="register-btn">Register device</button>
    <a class="button secondary" href="{u('/')}">Back to rides</a>
  </div>
  <p class="status" id="reg-status"></p>
</section>
<script>
const basePath = {json.dumps(BASE_PATH)};
document.getElementById('register-btn').onclick = async () => {{
  const camera_id = (document.getElementById('camera-id').value || '').trim();
  const status = document.getElementById('reg-status');
  if (!camera_id) {{ status.textContent = 'Enter a camera id first.'; return; }}
  const res = await fetch(basePath + '/api/devices', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{camera_id}}),
  }});
  const data = await res.json();
  if (!res.ok) {{ status.textContent = data.error || 'Register failed'; return; }}
  status.textContent = 'Registered ' + camera_id + '. It will show on rides that use this id.';
  const list = document.getElementById('reg-list');
  list.innerHTML = (data.registered || []).map(c => '<li><code>' + c + '</code></li>').join('') || '<li class="empty">None yet.</li>';
}};
</script>"""
    return page("Register device", body)


def ride_list(data: dict) -> bytes:
    incidents = data["incidents"]
    total = counts(incidents)
    registered = set(load_devices().get("registered") or [])
    cards = []
    for index, ride in enumerate(data["rides"]):
        mine = [item for item in incidents if item["ride"] == ride["id"]]
        found = counts(mine)
        unclear = f" · {found['unclear']} to decide" if found["unclear"] else ""
        situation = ride.get("situation") or ride.get("conditions") or ""
        cam = camera_id_for_ride(ride)
        reg_mark = " · registered" if cam in registered else ""
        thumb = RIDE_ART[index % len(RIDE_ART)]
        cards.append(
            f"""<a class="card with-art" href="{u('/ride/' + ride['id'])}">
              {art(thumb, ride_label(ride), "thumb")}
              <div>
                <div class="row"><h2>{esc(ride_label(ride))}</h2><span class="when">{esc(clock((ride.get('duration_min') or 0) * 60))} ride</span></div>
                <p class="summary">{esc(summary_line(found))}{esc(unclear)}</p>
                <p class="meta">{esc(situation)} <span class="cam">({esc(cam)}{esc(reg_mark)})</span></p>
              </div>
            </a>"""
        )
    body = f"""<div class="banner">{art("ground", "Flowers in the grass", "cover")}</div>
<h1>Your rides</h1>
<p id="page-audience">Helmet-cam moments worth a second look — reports you may send, and private lessons that stay with you.</p>
<p class="meta">{esc(summary_line(total))} across these rides · {esc(data_source())}</p>
<div class="toolbar">
  <a class="button" href="{u('/devices')}">Register device</a>
  <a class="button secondary" href="{u('/about')}">About / how it works</a>
</div>
<main class="rides">{''.join(cards)}</main>
{art("meadow", "A field of flowers under a blue sky", "flower-strip")}
<p class="foot">Camera ids label which physical stream filmed each ride. <a href="{u('/devices')}">Why so many ids?</a> · <a href="{u('/timeline')}">How the agent screened every segment</a></p>"""
    return page("Your rides", body, show_trees=False)


def effective_fault(item: dict) -> str:
    return item.get("review", {}).get("fault_override") or item.get("fault")


def incident_list(data: dict, ride_id: str, tab: str) -> bytes:
    ride = next((item for item in data["rides"] if item["id"] == ride_id), None)
    if ride is None:
        return page("Ride not found", f"<h1>Ride not found</h1><p><a href='{u('/')}'>Your rides</a></p>")
    mine = [item for item in data["incidents"] if item["ride"] == ride_id]
    mine.sort(key=lambda item: item.get("severity") or 0, reverse=True)
    found = counts(mine)
    if tab not in ("reports", "lessons"):
        tab = "reports"
    if tab == "reports":
        shown = [item for item in mine if effective_fault(item) in ("driver", "unclear", "shared")]
        empty = "No reports to send on this ride."
    else:
        shown = [item for item in mine if effective_fault(item) in ("rider", "unclear", "shared")]
        empty = "No lessons on this ride."
    rows = []
    for item in shown:
        fault = effective_fault(item)
        vehicle = item["facts"]["vehicle"]
        kind = vehicle["type"]
        who = "no vehicle" if kind == "none" else {"suv": "SUV"}.get(kind, kind)
        colour = f"{vehicle['colour']} " if vehicle.get("colour") else ""
        status = item.get("review", {}).get("status") or "pending"
        rows.append(
            f"""<a class="card" href="{u('/incident/' + item['id'])}">
              <div class="row">
                <h2>{esc(item['document']['title'])}</h2>
                <span class="badge {esc(fault)}">{esc(FAULT_LABEL.get(fault, fault))}</span>
              </div>
              <p>{esc(item['facts']['evidence'])}</p>
              <p class="when">{esc(clock(item['ride_offset_sec']))} into the ride · {esc(item['severity_label'])} · {esc(colour + who)} · {esc(status)}</p>
            </a>"""
        )
    listing = "".join(rows) if rows else f'<p class="empty">{empty}</p>'
    situation = ride.get("situation") or ride.get("conditions") or ""
    cam = camera_id_for_ride(ride)
    body = f"""<p class="kicker"><a href="{u('/')}">Your rides</a></p>
<h1>{esc(ride_label(ride))}</h1>
<p class="summary">{esc(summary_line(found))}</p>
<p class="meta">{esc(situation)} <span class="cam">({esc(cam)})</span></p>
{timeline_html(ride, mine)}
<p id="page-audience" class="hint">Audience: you. <b>Reports to send</b> are for police, 311, or your insurer — only if you approve. <b>Lessons for you</b> stay private.</p>
<nav class="tabs">
  <a class="{'on' if tab == 'reports' else ''}" href="{u('/ride/' + ride_id)}?tab=reports">Reports to send</a>
  <a class="{'on' if tab == 'lessons' else ''}" href="{u('/ride/' + ride_id)}?tab=lessons">Lessons for you</a>
</nav>
<main class="list">{listing}</main>"""
    return page(ride_label(ride), body)


def yn(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def vehicle_phrase(vehicle: dict) -> str:
    kind = vehicle.get("type") or "vehicle"
    if kind == "person":
        return "person on foot"
    if kind == "none":
        return "no motor vehicle"
    label = {"suv": "SUV"}.get(kind, kind)
    colour = (vehicle.get("colour") or "").strip()
    return f"{colour} {label}".strip() if colour else label


def place_phrase(position: object) -> str:
    return {
        "bike_lane": "in the bike lane",
        "traffic_lane": "in the traffic lane",
        "sidewalk": "on the sidewalk",
        "crosswalk": "in the crosswalk",
    }.get(str(position or ""), "on the road")


def signal_phrase(signal: object) -> str:
    return {
        "red": "The traffic signal facing the rider was red.",
        "green": "The traffic signal facing the rider was green.",
        "yellow": "The traffic signal facing the rider was yellow.",
        "none_visible": "No traffic signal was visible in the rider's direction.",
        "unknown": "The signal facing the rider could not be read clearly.",
    }.get(str(signal or ""), "")


def action_phrase(action: object, side: object) -> str:
    side_txt = {
        "left": "on the left",
        "right": "on the right",
        "front": "ahead",
        "none": "",
    }.get(str(side or ""), "")
    core = {
        "overtaking": "overtook the rider",
        "turning_across": "turned across the rider's path",
        "pulling_out": "pulled out into the rider's path",
        "crossing": "crossed in front of the rider",
        "stopped_in_lane": "was stopped in the rider's lane",
        "parallel_in_own_lane": "stayed alongside in its own lane",
        "none": None,
    }.get(str(action or ""), "moved near the rider")
    if core is None:
        return ""
    return f"{core}{(' ' + side_txt) if side_txt else ''}"


def distance_phrase(distance: object) -> str:
    return {
        "under_1m": "It came within about a metre.",
        "1_to_2m": "It passed within a couple of metres.",
        "over_2m": "It stayed more than about two metres away.",
        "unknown": "How close it got is unclear.",
    }.get(str(distance or ""), "")


def story_from_facts(item: dict) -> str:
    """Turn structured facts into a short plain-language paragraph for the UI."""
    facts = item.get("facts") or {}
    vehicle = facts.get("vehicle") or {}
    fault = effective_fault(item)
    checked = item.get("checked") or {}
    if checked.get("kind") == "person":
        parts = [checked.get("note") or "A person is close to the bike."]
        parts.append("Someone checked this clip by eye. It is flagged for review and does not decide who was at fault.")
        signal = signal_phrase(facts.get("signal_for_rider"))
        if signal:
            parts.append(signal)
        if fault == "unclear":
            parts.append("You decide whether this becomes a report or stays a private note.")
        return " ".join(p for p in parts if p)
    place = place_phrase(facts.get("rider_position"))
    who = vehicle_phrase(vehicle)
    action = action_phrase(facts.get("vehicle_action"), vehicle.get("side"))
    parts: list[str] = []

    if who == "no motor vehicle":
        parts.append(f"The rider is {place}.")
        evidence = (facts.get("evidence") or "").strip()
        if evidence:
            parts.append(evidence)
    else:
        if action:
            parts.append(f"The rider is {place} when a {who} {action}.")
        else:
            parts.append(f"The rider is {place} near a {who}.")

        if facts.get("separator") == "yes":
            parts.append("Something physical sat between them, such as posts or a curb.")
        elif facts.get("separator") == "no":
            parts.append("Nothing physical separated them beyond paint on the road.")

        if facts.get("vehicle_entered_rider_path") is True:
            parts.append("The vehicle moved into the rider's line.")
        if facts.get("rider_moved_into_vehicle_path") is True:
            parts.append("The rider also moved into the vehicle's path.")

        dist = distance_phrase(facts.get("closest_distance"))
        if dist and facts.get("closest_distance") != "over_2m":
            parts.append(dist)

        signal = signal_phrase(facts.get("signal_for_rider"))
        if signal:
            parts.append(signal)

        if facts.get("rider_entered_on_red") is True or (
            facts.get("signal_for_rider") == "red" and fault == "rider"
        ):
            parts.append("The rider appears to have entered while that signal was red.")

    if fault == "driver":
        parts.append("The rules treat this as driver fault.")
    elif fault == "unclear":
        parts.append("The boxes and the model disagree, so a person should decide.")
    elif fault == "shared":
        parts.append("Both sides look involved; the reviewer can choose the final call.")
    if checked.get("note"):
        parts.append(f"Checked by eye: {checked['note']}")

    return " ".join(p for p in parts if p)


def rider_reminders(item: dict) -> list[str]:
    """Short safety reminders for the rider. Not a legal claim and not a filing note."""
    facts = item.get("facts") or {}
    rule = item.get("fault_rule")
    signal = facts.get("signal_for_rider")
    pos = facts.get("rider_position")
    action = facts.get("vehicle_action")
    if (item.get("checked") or {}).get("kind") == "person":
        return [
            "Cover the brake when people are in the crosswalk or the lane.",
            "Give them room, especially if they are looking at a phone.",
            "Wait until the way ahead is clear before you roll through.",
        ]
    if rule == 3 or signal == "red":
        return [
            "Stop when the signal facing you is red.",
            "Wait for green before you enter. Do not follow another rider through.",
            "Look left, ahead, then right again for turning traffic.",
            "If you are already in the intersection when it changes, clear it; do not start a new entry.",
        ]
    if rule == 4 or pos == "sidewalk":
        return [
            "Ride in the bike lane or the traffic lane, not on the sidewalk.",
            "If you must leave the lane, slow to walking pace around people.",
            "Rejoin the road only when you can see a gap, and signal before you move.",
        ]
    if rule == 5 or facts.get("rider_moved_into_vehicle_path"):
        return [
            "Look over your shoulder before you change line.",
            "Signal, then move once, and hold the new line.",
            "Do not drift into a vehicle that is already beside you.",
        ]
    if rule == 6:
        return [
            "Leave a door's width when you pass parked cars.",
            "Watch for a driver in the seat or a door starting to open.",
            "If the gap is tight, slow down rather than squeeze through.",
        ]
    if action == "overtaking":
        return [
            "Hold your line so the driver can judge where you are.",
            "Glance for the pass, and leave yourself room to move left if it is too close.",
        ]
    return [
        "Scan early: signal, mirrors, and the gap in front of you.",
        "Hold a predictable line instead of swerving late.",
        "Leave an escape gap so you can brake or move without hitting the curb.",
    ]


def audience_copy(fault: str) -> tuple[str, str]:
    """Return (heading, paragraph) for who this review sheet is for."""
    if fault == "rider":
        return (
            "Your private review",
            "Audience: you only. This is a rider lesson about a risk you took. "
            "It stays in this app — there is no send action, and it is never a police report.",
        )
    if fault in ("unclear", "shared"):
        return (
            "Your review — you decide",
            "Audience: you, the cyclist. The agent is unsure who was at fault. "
            "Choose Driver if you want a report you may send to police, 311, or your insurer. "
            "Choose Rider if this should stay a private lesson.",
        )
    return (
        "Your review before sending",
        "Audience: you, the cyclist. If you approve, this becomes an authority report "
        "you can export for police, NYC 311, or your insurer. Nothing is sent automatically.",
    )


def incident_detail(data: dict, incident_id: str) -> bytes:
    item = next((row for row in data["incidents"] if row["id"] == incident_id), None)
    if item is None:
        return page("Incident not found", f"<h1>Incident not found</h1><p><a href='{u('/')}'>Your rides</a></p>")
    ride = next((r for r in data["rides"] if r["id"] == item["ride"]), {"id": item["ride"], "title": item["ride"]})
    facts = item["facts"]
    geom = item.get("geometry") or {}
    review = item.get("review") or {}
    fault = effective_fault(item)
    rule_fault = item.get("fault")
    model_fault = facts.get("model_fault")
    agree = item.get("model_agreement")
    agree_cls = "yes" if agree else "no"
    agree_txt = (
        "Rule and model agree"
        if agree
        else f"Rule says {FAULT_LABEL.get(rule_fault, rule_fault)}; model said {FAULT_LABEL.get(model_fault, model_fault)}"
    )
    source = item.get("preview_source") or ""
    bbox = geom.get("bbox")
    bbox_json = json.dumps(bbox)
    vehicle = facts.get("vehicle") or {}
    doc_kind = (item.get("document") or {}).get("kind")
    doc_link = ""
    if doc_kind == "authority_report" or fault == "driver":
        doc_link = f'<a class="button secondary" href="{u("/report/" + item["id"])}">Authority report</a>'
    if doc_kind == "rider_lesson" or fault == "rider":
        doc_link += f'<a class="button secondary" href="{u("/lesson/" + item["id"])}">Rider lesson</a>'
    if fault in ("unclear", "shared"):
        doc_link = (
            f'<a class="button secondary" href="{u("/report/" + item["id"])}">Authority report</a>'
            f'<a class="button secondary" href="{u("/lesson/" + item["id"])}">Rider lesson</a>'
        )

    fact_rows = [
        ("Event", item.get("event_type")),
        ("Signal for rider", facts.get("signal_for_rider")),
        ("Rider position", facts.get("rider_position")),
        ("Separator", facts.get("separator")),
        ("Vehicle action", facts.get("vehicle_action")),
        ("Vehicle entered rider path", yn(facts.get("vehicle_entered_rider_path"))),
        ("Rider moved into vehicle path", yn(facts.get("rider_moved_into_vehicle_path"))),
        ("Vehicle", vehicle_phrase(vehicle) + (f" · {vehicle['side']}" if vehicle.get("side") else "")),
        ("Closest distance", facts.get("closest_distance")),
        ("Peak height", f"{(geom.get('peak_h') or 0) * 100:.0f}% of frame"),
        ("Entered corridor", yn(geom.get("entered_corridor"))),
    ]
    if geom.get("min_gap_m") is not None:
        fact_rows.append(("Closest gap (YOLO)", f"about {geom['min_gap_m']:.2f} m"))
    votes = item.get("votes") or {}
    if votes.get("near_miss"):
        fact_rows.append(("Cosmos checks", f"near miss {of_n(votes['near_miss'], True)}"
                          + (f" · entered path {of_n(votes.get('entered_path'), True)}" if votes.get("entered_path") else "")))
    if facts.get("still_t") is not None:
        fact_rows.append((f"Signal still ({facts['still_t']} s)", SIGNAL_WORD.get(facts.get("still_signal"), facts.get("still_signal"))))
    if fault != "rider":
        fact_rows.extend(
            [
                ("Rule", f"your review → {FAULT_LABEL.get(rule_fault, rule_fault)} (rules said "
                         f"{FAULT_LABEL.get((item.get('engine') or {}).get('fault'), '?')})"
                 if item.get("fault_rule") == 0 else f"#{item.get('fault_rule')} → {FAULT_LABEL.get(rule_fault, rule_fault)}"),
                ("Model fault", FAULT_LABEL.get(model_fault, model_fault)),
            ]
        )
    facts_html = "".join(
        f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>" for label, value in fact_rows
    )
    story = story_from_facts(item)
    reminders = rider_reminders(item) if fault == "rider" else []
    reminder_html = ""
    if reminders:
        items = "".join(f"<li>{esc(line)}</li>" for line in reminders)
        reminder_html = (
            '<div class="lesson-inline">'
            "<h3>For you, next time</h3>"
            f"<ul>{items}</ul>"
            '<p class="hint">A reminder for you. It stays in this app.</p>'
            "</div>"
        )
    agree_html = "" if fault == "rider" else f'<p class="agree {agree_cls}">{esc(agree_txt)}</p>'
    review_heading, review_audience = audience_copy(fault)
    status_now = review.get("status") or "pending"

    def status_btn_class(name: str) -> str:
        if status_now == name:
            return "on"
        return "secondary"

    plate_needed = fault in ("driver", "unclear", "shared")
    plate_label = "Vehicle license plate (optional)"
    plate_hint = (
        "Type the plate if you can read it on the clip (letters and numbers). "
        "Leave blank if you cannot see it — the agent never invents a plate."
        if plate_needed
        else "Not needed for a private lesson. Leave blank."
    )
    plate_ph = "e.g. ABC-1234" if plate_needed else "Leave blank"
    street_ph = "e.g. Broadway & W 45th, or Residence Inn sign"

    prev_item, next_item = neighbors(data, item)
    prev_link = (
        f'<a href="{u("/incident/" + prev_item["id"])}">← {esc((prev_item.get("document") or {}).get("title") or prev_item["id"])}</a>'
        if prev_item
        else "<span></span>"
    )
    next_link = (
        f'<a href="{u("/incident/" + next_item["id"])}">{esc((next_item.get("document") or {}).get("title") or next_item["id"])} →</a>'
        if next_item
        else "<span></span>"
    )
    lead_seg, peak_seg, after_seg = segment_neighbors(source)
    # #region agent log
    _dbg(
        "H8",
        "incident_detail",
        "segments",
        {
            "id": item.get("id"),
            "has_peak": peak_seg is not None,
            "has_lead": lead_seg is not None,
            "has_after": after_seg is not None,
            "chunk": (peak_seg or {}).get("chunk_index"),
            "seg": (peak_seg or {}).get("segment_number"),
        },
    )
    # #endregion
    lead_src = (lead_seg or {}).get("source") or ""
    peak_src = (peak_seg or {}).get("source") or source
    after_src = (after_seg or {}).get("source") or ""
    lead_disabled = "disabled" if not lead_src else ""
    after_disabled = "disabled" if not after_src else ""

    scene_key, scene_alt = scene_for_fault(fault, item)
    body = f"""<p class="kicker"><a href="{u('/ride/' + item['ride'])}">Back to {esc(ride_label(ride))}</a></p>
<div class="split">
  {art(scene_key, scene_alt, "figure")}
  <h1>{esc(item['document']['title'])}</h1>
</div>
<p><span class="badge {esc(fault)}" id="fault-badge">{esc(FAULT_LABEL.get(fault, fault))}</span>
<span class="when"> · {esc(item['severity_label'])} · {esc(clock(item['ride_offset_sec']))} into the ride · peak {esc(item.get('t_peak'))}s</span></p>
<p>{esc(facts['evidence'])}</p>
{agree_html}
<nav class="navrow">{prev_link}{next_link}</nav>

<main class="stage">
  <section class="panel">
    <h3>Clip</h3>
    <div class="player">
      <video id="v" controls playsinline preload="metadata"></video>
      <canvas id="c"></canvas>
    </div>
    <div class="segbar" id="segbar">
      <button type="button" data-role="lead" data-source="{esc(lead_src)}" {lead_disabled}>Lead-up</button>
      <button type="button" data-role="peak" data-source="{esc(peak_src)}" class="on">Peak</button>
      <button type="button" data-role="after" data-source="{esc(after_src)}" {after_disabled}>Aftermath</button>
    </div>
    <p class="hint">Lead-up and aftermath are the previous/next archive segments for this clip (by ride order), not guessed filenames.</p>
    <p class="status" id="play-status">Loading clip…</p>
  </section>
  <section class="panel">
    <h3>What happened</h3>
    <p class="story">{esc(story)}</p>
    {reminder_html}
    <details class="tech">
      <summary>Technical details</summary>
      <div class="facts">{facts_html}</div>
      <p class="when" style="margin-top:12px">{esc(item['source'])}</p>
    </details>
  </section>
</main>

<section class="panel" style="width:min(960px, calc(100% - 32px)); margin: 0 auto 40px;">
  <h3 id="review-heading">{esc(review_heading)}</h3>
  <p class="audience" id="review-audience">{esc(review_audience)}</p>
  <p class="hint">Who was at fault? Change this if you disagree with the agent.</p>
  <nav class="tabs" id="fault-tabs">
    <button type="button" data-fault="driver" class="{'on' if fault == 'driver' else ''}">Driver at fault</button>
    <button type="button" data-fault="rider" class="{'on' if fault == 'rider' else ''}">I was at fault</button>
    <button type="button" data-fault="unclear" class="{'on' if fault == 'unclear' else ''}">Unclear</button>
    <button type="button" data-fault="shared" class="{'on' if fault == 'shared' else ''}">Shared</button>
  </nav>
  <label>{esc(plate_label)}
    <span class="hint">{esc(plate_hint)}</span>
    <input id="plate" value="{esc(review.get('plate') or '')}" placeholder="{esc(plate_ph)}">
  </label>
  <label>Where did this happen?
    <span class="hint">Street, intersection, or a landmark you recognize — the footage has no GPS.</span>
    <input id="street" value="{esc(review.get('street') or '')}" placeholder="{esc(street_ph)}">
  </label>
  <div class="actions" id="status-actions">
    <button type="button" id="approve" data-status="approved" class="{esc(status_btn_class('approved'))}">Approve</button>
    <button type="button" id="dismiss" data-status="dismissed" class="{esc(status_btn_class('dismissed'))}">Dismiss</button>
    <button type="button" id="save" data-status="edited" class="{esc(status_btn_class('edited'))}">Save edits</button>
    {doc_link}
  </div>
  <p class="status" id="review-status">Status: {esc(status_now)}</p>
</section>

<script>
const basePath = {json.dumps(BASE_PATH)};
const incidentId = {json.dumps(item["id"])};
const peakSource = {json.dumps(peak_src)};
const bbox = {bbox_json};
const peak = {json.dumps(item.get("t_peak") or 0)};
const v = document.getElementById('v');
const c = document.getElementById('c');
const status = document.getElementById('play-status');
const reviewStatus = document.getElementById('review-status');
let chosenFault = {json.dumps(fault)};
let activeRole = 'peak';

function sizeCanvas() {{
  c.width = v.clientWidth * devicePixelRatio;
  c.height = v.clientHeight * devicePixelRatio;
  c.style.width = v.clientWidth + 'px';
  c.style.height = v.clientHeight + 'px';
  draw();
}}

function draw() {{
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  if (!bbox || activeRole !== 'peak') return;
  const [x1, y1, x2, y2] = bbox;
  ctx.strokeStyle = '#f2c14e';
  ctx.lineWidth = 3 * devicePixelRatio;
  ctx.strokeRect(x1 * c.width, y1 * c.height, (x2 - x1) * c.width, (y2 - y1) * c.height);
}}

function loadClip(src, role) {{
  activeRole = role;
  document.querySelectorAll('#segbar button').forEach(b => b.classList.toggle('on', b.dataset.role === role));
  if (!src) {{
    status.textContent = 'No segment for ' + role + '.';
    return;
  }}
  status.textContent = 'Loading ' + role + '…';
  v.src = basePath + '/api/stream?source=' + encodeURIComponent(src);
}}

v.addEventListener('loadedmetadata', () => {{
  const label = activeRole === 'peak' ? `Peak at ${{peak}} s` : activeRole;
  status.textContent = `Loaded ${{v.duration.toFixed(1)}} s · ${{label}}`;
  sizeCanvas();
  if (activeRole === 'peak') {{
    try {{ v.currentTime = Math.min(peak, Math.max(0, v.duration - 0.1)); }} catch (e) {{}}
  }}
}});
v.addEventListener('seeked', () => {{ sizeCanvas(); status.textContent = `At ${{v.currentTime.toFixed(1)}} s · ${{activeRole}}`; }});
v.addEventListener('play', sizeCanvas);
v.addEventListener('error', () => {{ status.textContent = 'Playback error — check /api/stream.'; }});
window.addEventListener('resize', sizeCanvas);
document.querySelectorAll('#segbar button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    if (btn.disabled) return;
    loadClip(btn.dataset.source || '', btn.dataset.role);
  }});
}});
if (peakSource) {{
  loadClip(peakSource, 'peak');
}} else {{
  status.textContent = 'No preview_source on this incident yet.';
}}

const faultLabels = {{driver: 'Driver', rider: 'Rider', unclear: 'Unclear', shared: 'Shared'}};
const audienceByFault = {{
  driver: ['Your review before sending', 'Audience: you, the cyclist. If you approve, this becomes an authority report you can export for police, NYC 311, or your insurer. Nothing is sent automatically.'],
  rider: ['Your private review', 'Audience: you only. This is a rider lesson about a risk you took. It stays in this app — there is no send action, and it is never a police report.'],
  unclear: ['Your review — you decide', 'Audience: you, the cyclist. The agent is unsure who was at fault. Choose Driver if you want a report you may send. Choose Rider if this should stay a private lesson.'],
  shared: ['Your review — you decide', 'Audience: you, the cyclist. Both sides look involved. Choose whether this should become a report to send, or a private lesson.'],
}};
document.querySelectorAll('#fault-tabs button').forEach(btn => {{
  btn.addEventListener('click', () => {{
    chosenFault = btn.dataset.fault;
    document.querySelectorAll('#fault-tabs button').forEach(b => b.classList.toggle('on', b === btn));
    const badge = document.getElementById('fault-badge');
    badge.className = 'badge ' + chosenFault;
    badge.textContent = faultLabels[chosenFault] || chosenFault;
    const pair = audienceByFault[chosenFault];
    if (pair) {{
      document.getElementById('review-heading').textContent = pair[0];
      document.getElementById('review-audience').textContent = pair[1];
    }}
  }});
}});

function highlightStatus(statusValue) {{
  document.querySelectorAll('#status-actions button[data-status]').forEach(b => {{
    const active = b.dataset.status === statusValue;
    b.classList.toggle('on', active);
    b.classList.toggle('secondary', !active);
  }});
}}

async function review(statusValue) {{
  const body = {{
    id: incidentId,
    status: statusValue,
    plate: document.getElementById('plate').value,
    street: document.getElementById('street').value,
    fault_override: chosenFault,
  }};
  const res = await fetch(basePath + '/api/review', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }});
  const data = await res.json();
  if (!res.ok) {{
    reviewStatus.textContent = data.error || 'Review failed';
    return;
  }}
  highlightStatus(data.review.status);
  reviewStatus.textContent = 'Status: ' + data.review.status + (data.review.fault_override ? ' · fault ' + data.review.fault_override : '');
}}

document.getElementById('approve').onclick = () => review('approved');
document.getElementById('dismiss').onclick = () => review('dismissed');
document.getElementById('save').onclick = () => review('edited');
</script>
"""
    return page(item["id"], body)


def neighbors(data: dict, item: dict) -> tuple[dict | None, dict | None]:
    mine = [row for row in data["incidents"] if row["ride"] == item["ride"]]
    mine.sort(key=lambda row: (row.get("ride_offset_sec") or 0, row.get("id") or ""))
    ids = [row["id"] for row in mine]
    try:
        idx = ids.index(item["id"])
    except ValueError:
        return None, None
    prev_item = mine[idx - 1] if idx > 0 else None
    next_item = mine[idx + 1] if idx + 1 < len(mine) else None
    return prev_item, next_item


def timeline_html(ride: dict, incidents: list[dict]) -> str:
    duration = max((ride.get("duration_min") or 15) * 60, 1)
    marks = []
    for item in sorted(incidents, key=lambda row: row.get("ride_offset_sec") or 0):
        fault = effective_fault(item)
        pct = min(98, max(1, ((item.get("ride_offset_sec") or 0) / duration) * 100))
        title = (item.get("document") or {}).get("title") or item["id"]
        marks.append(
            f'<a class="mark {esc(fault)}" style="left:{pct:.1f}%" '
            f'href="{u("/incident/" + item["id"])}" title="{esc(title)}"></a>'
        )
    if not marks:
        return ""
    return (
        '<div class="timeline" aria-label="Severity timeline">'
        '<div class="track"></div>'
        + "".join(marks)
        + "</div>"
        '<p class="hint">Timeline: red = driver report, amber = rider lesson, grey = unclear.</p>'
    )


def lesson_sections(item: dict) -> dict[str, str]:
    doc = item.get("document") or {}
    if doc.get("what_happened") and doc.get("why_risky") and doc.get("next_time"):
        return {
            "what_happened": doc["what_happened"],
            "why_risky": doc["why_risky"],
            "next_time": doc["next_time"],
        }
    facts = item.get("facts") or {}
    story = story_from_facts(item)
    pos = facts.get("rider_position")
    signal = facts.get("signal_for_rider")
    why = "A tight gap with traffic leaves little time to brake or change line."
    if (item.get("checked") or {}).get("kind") == "person":
        why = "People in the roadway or the crosswalk are close to the bike. Give them room and cover the brake."
    elif pos == "sidewalk":
        why = "People on the sidewalk do not expect a bike, and rejoining the road means a sudden move into traffic."
    elif signal == "red":
        why = "Crossing traffic still has the green. Entering on red puts you in their path."
    elif facts.get("vehicle_entered_rider_path"):
        why = "A vehicle in your line leaves little room to brake or swerve."
    reminders = rider_reminders(item)
    return {
        "what_happened": story,
        "why_risky": why,
        "next_time": " ".join(reminders),
        "reminders": reminders,
    }


def report_summary(item: dict) -> str:
    doc = item.get("document") or {}
    if doc.get("summary"):
        return str(doc["summary"])
    return story_from_facts(item)


RULE_TEXT = {
    0: "Your review in this app; it replaces the rules' verdict when the engine rebuilds.",
    3: "The signal facing the rider was red as they entered.",
    4: "The rider was on the sidewalk.",
    5: "The rider changed line into the vehicle's path.",
    7: "The vehicle entered the rider's path with no barrier between them and no red signal for the rider; "
       "Cosmos and YOLO both saw it.",
    8: "Cosmos and YOLO disagree, so a person decides.",
    9: "No signal was visible on a crossing; the rider confirms right of way.",
}
SIGNAL_WORD = {"red": "red", "green": "green", "yellow": "yellow", "none_visible": "none visible", "unknown": "unclear"}


def of_n(values: list | None, want: object) -> str:
    if not values:
        return ""
    return f"{sum(v == want for v in values)} of {len(values)}"


def evidence_rows(item: dict) -> list[tuple[str, str]]:
    facts = item.get("facts") or {}
    geom = item.get("geometry") or {}
    votes = item.get("votes") or {}
    rows: list[tuple[str, str]] = []
    checked = item.get("checked") or {}
    if checked.get("note"):
        rows.append(("Checked by eye",
                     f"{checked.get('label')}: {checked['note']} "
                     "This flags the clip for review. It does not by itself decide who was at fault."))
    yolo_side = facts.get("yolo_side")
    if yolo_side and (facts.get("vehicle") or {}).get("side") and yolo_side != facts["vehicle"]["side"]:
        rows.append(("Which side",
                     f"The eye check places the vehicle on the {facts['vehicle']['side']}. "
                     f"The largest measured box is on the {yolo_side}."))
    if geom.get("min_gap_m") is not None:
        rows.append(("Closest gap (YOLO)", f"about {geom['min_gap_m']:.2f} m from the rider's line (estimated from box size)"))
    if geom.get("peak_h"):
        rows.append(("Vehicle size at peak", f"{geom['peak_h'] * 100:.0f}% of frame height at {item.get('t_peak')} s"))
    if geom.get("lateral_toward_rider"):
        rows.append(("Moving toward the rider", f"{geom['lateral_toward_rider']:.1f} m/s sideways"))
    if votes.get("near_miss"):
        line = f"near miss in {of_n(votes['near_miss'], True)} independent checks"
        if votes.get("entered_path"):
            line += f"; vehicle entered the rider's path in {of_n(votes['entered_path'], True)}"
        rows.append(("Cosmos review", line))
    dist = distance_phrase(facts.get("closest_distance"))
    if dist:
        rows.append(("Cosmos distance", dist))
    still = facts.get("still_signal")
    if still and facts.get("still_t") is not None:
        agree = of_n(votes.get("still_signal"), still)
        rows.append((f"Signal in still at {facts['still_t']} s",
                     SIGNAL_WORD.get(still, still) + (f" ({agree} checks)" if agree else "")))
    if facts.get("separator") in ("yes", "no"):
        rows.append(("Physical barrier", "yes" if facts["separator"] == "yes" else "none (a painted line does not count)"))
    if item.get("low_light"):
        rows.append(("Light", "low light: distances and colours are less reliable"))
    rule = item.get("fault_rule")
    if rule == 0:
        engine = item.get("engine") or {}
        rows.append(("Decided by your review", f"the rules said {FAULT_LABEL.get(engine.get('fault'), engine.get('fault'))}"
                     f" (rule {engine.get('rule')})"))
    elif rule in RULE_TEXT:
        rows.append((f"Decided by rule {rule}", RULE_TEXT[rule]))
    return rows


def evidence_html(item: dict) -> str:
    """Key frames cut client-side from the clip (box drawn on the peak), signal still, and the numbers behind the verdict."""
    facts = item.get("facts") or {}
    t_peak = item.get("t_peak") or 0
    times = item.get("evidence_frames") or []
    peak_t = min(times, key=lambda t: abs(t - t_peak)) if times else None
    frames = [{"t": t, "box": t == peak_t,
               "label": "peak · box = flagged vehicle" if t == peak_t else "before" if t < t_peak else "after"}
              for t in times]
    if facts.get("still_t") is not None and facts.get("still_signal") not in (None, "unknown"):
        frames.append({"t": facts["still_t"], "box": False,
                       "label": f"signal still · {SIGNAL_WORD.get(facts['still_signal'], facts['still_signal'])}"})
    rows = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in evidence_rows(item))
    figs = "".join(f'<figure><canvas data-i="{i}"></canvas><figcaption>{esc(f["t"])} s · {esc(f["label"])}</figcaption></figure>'
                   for i, f in enumerate(frames))
    return f"""
<style>
  .frames {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; margin: 10px 0 14px; }}
  .frames figure {{ margin: 0; }}
  .frames canvas {{ width: 100%; aspect-ratio: 16 / 9; background: #111; display: block; }}
  .frames figcaption {{ font: 12px/1.3 ui-sans-serif, system-ui, sans-serif; color: var(--muted); margin-top: 4px; }}
  .ev-grid {{ display: grid; gap: 0; margin: 8px 0 12px; font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; }}
  .ev-grid div {{ display: grid; grid-template-columns: 11rem 1fr; gap: 10px; border-bottom: 1px solid var(--line); padding: 7px 0; }}
  .ev-grid dt {{ color: var(--muted); }}
  .ev-grid dd {{ margin: 0; }}
  @media print {{ .frames {{ grid-template-columns: repeat(4, 1fr); }} }}
</style>
<h2>Evidence</h2>
<div class="frames" id="frames">{figs}</div>
<dl class="ev-grid">{rows}</dl>
<p class="meta">Closeness is estimated from the size of the vehicle's box, not measured. Frames are taken from the clip itself.</p>
<script>
(() => {{
  const frames = {json.dumps(frames)};
  const bbox = {json.dumps((item.get("geometry") or {}).get("bbox"))};
  const src = {json.dumps(u('/api/stream'))} + '?source=' + encodeURIComponent({json.dumps(item.get("preview_source") or "")});
  const grab = (i) => {{
    if (i >= frames.length) return;
    const f = frames[i];
    const v = document.createElement('video');
    v.muted = true; v.preload = 'auto'; v.playsInline = true;
    v.addEventListener('loadeddata', () => {{ v.currentTime = Math.min(f.t, Math.max(0, v.duration - 0.05)); }}, {{once: true}});
    v.addEventListener('seeked', () => {{
      const c = document.querySelector('#frames canvas[data-i="' + i + '"]');
      c.width = v.videoWidth; c.height = v.videoHeight;
      const ctx = c.getContext('2d');
      ctx.drawImage(v, 0, 0);
      if (f.box && bbox) {{
        const [x1, y1, x2, y2] = bbox;
        ctx.strokeStyle = '#f2c14e'; ctx.lineWidth = Math.max(4, c.width / 240);
        ctx.strokeRect(x1 * c.width, y1 * c.height, (x2 - x1) * c.width, (y2 - y1) * c.height);
      }}
      v.removeAttribute('src'); v.load();
      grab(i + 1);
    }}, {{once: true}});
    v.addEventListener('error', () => grab(i + 1), {{once: true}});
    v.src = src;
  }};
  grab(0);
}})();
</script>"""


def authority_report_page(data: dict, incident_id: str) -> bytes:
    item = next((row for row in data["incidents"] if row["id"] == incident_id), None)
    if item is None:
        return page("Not found", f"<h1>Not found</h1><p><a href='{u('/')}'>Your rides</a></p>")
    ride = next((r for r in data["rides"] if r["id"] == item["ride"]), {"title": item["ride"]})
    facts = item.get("facts") or {}
    vehicle = facts.get("vehicle") or {}
    review = item.get("review") or {}
    fault = effective_fault(item)
    if fault == "rider":
        return page(
            "Not a report",
            f"<h1>This is a private lesson</h1>"
            f"<p class='audience'>Audience: you only. Rider-fault moments do not produce an authority report.</p>"
            f"<p><a href='{u('/lesson/' + incident_id)}'>Open the rider lesson</a> · "
            f"<a href='{u('/incident/' + incident_id)}'>Back to incident</a></p>",
        )
    colour = (vehicle.get("colour") or "").strip()
    kind = vehicle.get("type") or "vehicle"
    if kind == "person":
        vehicle_line = "Person on foot — the eye check did not name a motor vehicle as the close party"
    else:
        kind = {"suv": "SUV"}.get(kind, kind)
        vehicle_line = f"{colour} {kind}".strip() if colour else f"{kind} (colour: cyclist to confirm)"
    plate = review.get("plate") or "— (cyclist to confirm)"
    street = review.get("street") or "— (cyclist to confirm)"
    recorded = review.get("recorded_at") or "unknown — cyclist to confirm"
    body = f"""
<style>
  .doc {{ width: min(720px, calc(100% - 32px)); margin: 0 auto 48px; }}
  .doc .panel {{ padding: 22px; }}
  .doc h1 {{ font-size: 26px; }}
  .form-grid {{ display: grid; gap: 10px; margin: 18px 0; font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; }}
  .form-grid div {{ display: grid; grid-template-columns: 11rem 1fr; gap: 10px; border-bottom: 1px solid var(--line); padding: 8px 0; }}
  .form-grid dt {{ color: var(--muted); }}
  .printbar {{ display: flex; gap: 8px; margin: 16px 0; }}
  .printbar button, .printbar a {{
    font: 14px/1 ui-sans-serif, system-ui, sans-serif;
    border: 1px solid var(--ink); background: var(--ink); color: var(--paper);
    padding: 10px 12px; text-decoration: none; cursor: pointer;
  }}
  .printbar a.secondary, .printbar button.secondary {{ background: transparent; color: var(--ink); }}
  @media print {{
    body {{ background: white; }}
    .kicker, .printbar, .noprint {{ display: none !important; }}
    .doc .panel {{ border: none; padding: 0; }}
  }}
</style>
<p class="kicker noprint"><a href="{u('/incident/' + incident_id)}">Back to incident</a></p>
<div class="doc">
  <div class="printbar noprint">
    <button type="button" onclick="window.print()">Print / save PDF</button>
    <button type="button" class="secondary" id="send-dummy">Send (demo only)</button>
    <a class="secondary" href="{u('/incident/' + incident_id)}">Edit review</a>
  </div>
  <section class="panel">
    <div class="split noprint">{art("police", "Police car")}<p class="kicker" style="margin:0;">Authority report · audience: police / 311 / insurer (only if you export)</p></div>
    <h1>{esc((item.get('document') or {}).get('title') or 'Incident report')}</h1>
    <p class="audience">Prepared for you, the cyclist, to review. Nothing is submitted until you export or use the demo Send action.</p>
    <p>{esc(report_summary(item))}</p>
    <div class="form-grid">
      <div><dt>Ride</dt><dd>{esc(ride_label(ride))}</dd></div>
      <div><dt>Offset in ride</dt><dd>{esc(clock(item.get('ride_offset_sec') or 0))}</dd></div>
      <div><dt>Recorded</dt><dd>{esc(recorded)}</dd></div>
      <div><dt>Location</dt><dd>{esc(street)}</dd></div>
      <div><dt>Vehicle</dt><dd>{esc(vehicle_line)} · side {esc(vehicle.get('side') or 'unknown')}</dd></div>
      <div><dt>License plate</dt><dd>{esc(plate)}</dd></div>
      <div><dt>Event type</dt><dd>{esc(str(item.get('event_type') or '').replace('_', ' '))}</dd></div>
      <div><dt>Severity</dt><dd>{esc(item.get('severity_label'))} ({esc(item.get('severity'))})</dd></div>
      <div><dt>Fault</dt><dd>{esc(FAULT_LABEL.get(fault, fault))}</dd></div>
      <div><dt>Signal for rider</dt><dd>{esc(SIGNAL_WORD.get(facts.get('signal_for_rider'), facts.get('signal_for_rider')))}</dd></div>
      <div><dt>Rider position</dt><dd>{esc(str(facts.get('rider_position') or '').replace('_', ' '))}</dd></div>
      <div><dt>Clip</dt><dd>{esc(item.get('source'))}</dd></div>
    </div>
    {evidence_html(item)}
    <p class="meta">Recording date/time and GPS are not in the archive; confirm them before filing.</p>
    <p class="status" id="send-status"></p>
  </section>
</div>
<script>
document.getElementById('send-dummy').onclick = async () => {{
  const res = await fetch({json.dumps(u('/api/submit'))}, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id: {json.dumps(incident_id)}, kind: 'authority_report'}}),
  }});
  const data = await res.json();
  document.getElementById('send-status').textContent = data.ok
    ? 'Demo send recorded in the dummy outbox. Nothing went to police or 311.'
    : (data.error || 'Send failed');
}};
</script>
"""
    return page("Authority report", body)


def lesson_page(data: dict, incident_id: str) -> bytes:
    item = next((row for row in data["incidents"] if row["id"] == incident_id), None)
    if item is None:
        return page("Not found", f"<h1>Not found</h1><p><a href='{u('/')}'>Your rides</a></p>")
    sections = lesson_sections(item)
    fault = effective_fault(item)
    if fault == "driver":
        return page(
            "Not a lesson",
            f"<h1>This is a report to review</h1>"
            f"<p class='audience'>Audience: you may send this onward. Open the authority report instead of a lesson.</p>"
            f"<p><a href='{u('/report/' + incident_id)}'>Open the authority report</a> · "
            f"<a href='{u('/incident/' + incident_id)}'>Back to incident</a></p>",
        )
    body = f"""
<style>
  .doc {{ width: min(720px, calc(100% - 32px)); margin: 0 auto 48px; }}
  .lesson-card {{ border: 1px solid var(--line); background: var(--card); padding: 22px; }}
  .lesson-card h2 {{ margin: 18px 0 6px; font-size: 18px; }}
  .reminders, .lesson-inline ul {{ margin: 8px 0 0; padding-left: 1.2rem; }}
  .lesson-inline {{ margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }}
  .lesson-inline h3 {{ margin-bottom: 6px; }}
  .printbar {{ display: flex; gap: 8px; margin: 16px 0; }}
  .printbar button, .printbar a {{
    font: 14px/1 ui-sans-serif, system-ui, sans-serif;
    border: 1px solid var(--ink); background: transparent; color: var(--ink);
    padding: 10px 12px; text-decoration: none; cursor: pointer;
  }}
  @media print {{
    .kicker, .printbar, .noprint {{ display: none !important; }}
    .lesson-card {{ border: none; padding: 0; }}
  }}
</style>
<p class="kicker noprint"><a href="{u('/incident/' + incident_id)}">Back to incident</a></p>
<div class="doc">
  <div class="printbar noprint">
    <button type="button" onclick="window.print()">Print for yourself</button>
    <a href="{u('/incident/' + incident_id)}">Edit review</a>
  </div>
  <section class="lesson-card">
    <div class="split">
      {art("signal" if (item.get("facts") or {}).get("signal_for_rider") == "red" else "bike", "How to ride this moment")}
      <div>
        <p class="kicker">For you</p>
        <h1>{esc((item.get('document') or {}).get('title') or 'Rider lesson')}</h1>
      </div>
    </div>
    <h2>What happened</h2>
    <p>{esc(sections['what_happened'])}</p>
    <h2>Why it was risky</h2>
    <p>{esc(sections['why_risky'])}</p>
    <h2>Next time</h2>
    <ul class="reminders">{''.join(f'<li>{esc(line)}</li>' for line in sections.get('reminders') or [sections['next_time']])}</ul>
    {evidence_html(item)}
    <p class="meta">A reminder for how to ride this spot again. General safety advice only.</p>
  </section>
</div>
"""
    return page("Rider lesson", body)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = strip_base(parsed.path.rstrip("/") or "/")
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/health":
            data = load_data()
            return self._json(
                200,
                {
                    "ok": True,
                    "data_source": data_source(),
                    "incidents": len(data.get("incidents") or []),
                    "segments": len(load_segments()),
                    "base_path": BASE_PATH,
                },
            )
        if path == "/api/incidents":
            return self._json(200, load_data())
        if path == "/api/stream":
            return self.stream(query.get("source", [""])[0])
        if path == "/api/detections":
            return self.detections(query.get("source", [""])[0])
        if path.startswith("/static/"):
            key = path[len("/static/") :]
            spec = ART.get(key)
            if spec is None:
                return self._send(404, b"not found", "text/plain")
            filename, ctype = spec
            file_path = ROOT / filename
            if not file_path.is_file():
                return self._send(404, b"not found", "text/plain")
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        if path == "/api/rides":
            return self._json(200, ride_summaries(*load_rides()))
        if path.startswith("/api/rides/"):
            rides, screen = load_rides()
            ride = urllib.parse.unquote(path[len("/api/rides/"):])
            if ride not in rides:
                return self._json(404, {"error": f"unknown ride {ride}"})
            return self._json(200, {"ride": ride, "legend": screen.get("legend", {}), "segments": rides[ride]})
        if path == "/timeline":
            html = (ROOT / "timeline.html").read_text().replace("__BASE__", u("/"))
            return self._send(200, html.encode(), "text/html; charset=utf-8")

        data = load_data()
        # #region agent log
        _dbg(
            "H6",
            "Handler.do_GET",
            "route",
            {"path": path, "base_path": BASE_PATH, "incidents": len(data.get("incidents") or []), "data_source": data_source()},
        )
        # #endregion
        if path == "/":
            body = ride_list(data)
            # #region agent log
            sample = next((r["id"] for r in data.get("rides") or []), "")
            want = u(f"/ride/{sample}").encode() if sample else b""
            _dbg(
                "H6",
                "Handler.do_GET:home",
                "prefixed_links",
                {
                    "base_path": BASE_PATH,
                    "sample_href_ok": (want in body) if want else False,
                    "bare_ride_href": b'href="/ride/' in body,
                },
            )
            # #endregion
        elif path == "/about":
            body = about_page(data)
        elif path == "/devices":
            body = devices_page(data)
            # #region agent log
            _dbg(
                "H9",
                "Handler.do_GET:devices",
                "page",
                {
                    "known": cameras_in_data(data),
                    "registered": load_devices().get("registered") or [],
                },
            )
            # #endregion
        elif path.startswith("/ride/"):
            ride_id = path.split("/", 2)[2]
            body = incident_list(data, ride_id, (query.get("tab") or ["reports"])[0])
            # #region agent log
            _dbg(
                "H2",
                "Handler.do_GET:ride",
                "timeline",
                {
                    "ride_id": ride_id,
                    "has_timeline": b'class="timeline"' in body,
                    "tab": (query.get("tab") or ["reports"])[0],
                    "bare_incident_href": b'href="/incident/' in body,
                },
            )
            # #endregion
        elif path.startswith("/incident/"):
            body = incident_detail(data, path.split("/", 2)[2])
            # #region agent log
            _dbg(
                "H3",
                "Handler.do_GET:incident",
                "nav",
                {
                    "has_navrow": b'class="navrow"' in body,
                    "has_report_or_lesson": u("/report/").encode() in body or u("/lesson/").encode() in body,
                    "api_stream_prefixed": (BASE_PATH.encode() + b"/api/stream") in body if BASE_PATH else b"/api/stream" in body,
                },
            )
            # #endregion
        elif path.startswith("/report/"):
            body = authority_report_page(data, path.split("/", 2)[2])
            # #region agent log
            _dbg("H4", "Handler.do_GET:report", "page", {"has_send": b"send-dummy" in body, "title": b"Authority report" in body})
            # #endregion
        elif path.startswith("/lesson/"):
            body = lesson_page(data, path.split("/", 2)[2])
            # #region agent log
            _dbg(
                "H5",
                "Handler.do_GET:lesson",
                "page",
                {"has_sections": b"What happened" in body and b"Next time" in body, "no_send": b"send-dummy" not in body},
            )
            # #endregion
        else:
            body = page("Not found", f"<h1>Not found</h1><p><a href='{u('/')}'>All rides</a></p>")
            return self._send(404, body, "text/html; charset=utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = strip_base(parsed.path.rstrip("/") or "/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        if path == "/api/review":
            return self.review(payload)
        if path == "/api/submit":
            return self._json(200, {"ok": True, "outbox": "dummy", "received": payload})
        if path == "/api/devices":
            camera_id = (payload.get("camera_id") or "").strip()
            if not camera_id:
                return self._json(400, {"error": "camera_id required"})
            if len(camera_id) > 120 or any(ch in camera_id for ch in "<>\"'\\"):
                return self._json(400, {"error": "invalid camera_id"})
            devices = load_devices()
            registered = devices.get("registered") or []
            if camera_id not in registered:
                registered.append(camera_id)
            devices["registered"] = registered
            save_devices(devices)
            # #region agent log
            _dbg("H9", "Handler.do_POST:devices", "registered", {"camera_id": camera_id, "count": len(registered)})
            # #endregion
            return self._json(200, {"ok": True, "registered": registered})
        self._json(404, {"error": "not found"})

    def review(self, payload: dict) -> None:
        incident_id = payload.get("id")
        if not incident_id:
            return self._json(400, {"error": "id required"})
        data = load_data()
        if not any(item["id"] == incident_id for item in data["incidents"]):
            return self._json(404, {"error": "unknown incident"})
        reviews = load_reviews()
        current = reviews.get(incident_id, {})
        current.update(
            {
                "status": payload.get("status") or current.get("status") or "edited",
                "plate": payload.get("plate", current.get("plate", "")),
                "street": payload.get("street", current.get("street", "")),
                "fault_override": payload.get("fault_override") or current.get("fault_override"),
            }
        )
        reviews[incident_id] = current
        save_reviews(reviews)
        self._json(200, {"ok": True, "review": current})

    def stream(self, source: str) -> None:
        cfg = load_cfg()
        allowed = (
            f"s3://{cfg['username']}-vss-chunks-segments/",
            f"s3://{cfg['username']}-vss-chunks/",
        )
        if not source.startswith(allowed):
            return self._json(400, {"error": "source must be an s3:// URI in this team's chunk buckets"})
        fwd = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            upstream = vss_open("/api/v1/videos/stream", {"source": source}, fwd)
        except urllib.error.HTTPError as exc:
            return self._json(exc.code, {"error": f"backend returned {exc.code}"})
        except (urllib.error.URLError, TimeoutError) as exc:
            return self._json(502, {"error": f"backend unreachable: {exc}"})
        with upstream:
            self.send_response(upstream.status)
            self.send_header(
                "Content-Type",
                "video/mp4" if source.endswith(".mp4") else upstream.headers.get("Content-Type", "application/octet-stream"),
            )
            for header in PASS_THROUGH_HEADERS:
                if upstream.headers.get(header):
                    self.send_header(header, upstream.headers[header])
            if not upstream.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            if not upstream.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                shutil.copyfileobj(upstream, self.wfile, 64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def detections(self, source: str) -> None:
        if not source:
            return self._json(400, {"error": "source required"})
        try:
            payload = vss_get_json("/api/v1/videos/detections", {"source": source})
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return self._json(404, {"error": "no detections"})
            return self._json(exc.code, {"error": f"backend returned {exc.code}"})
        except (urllib.error.URLError, TimeoutError) as exc:
            return self._json(502, {"error": f"backend unreachable: {exc}"})
        self._json(200, payload)

    def _send(self, code: int, body: bytes | str, ctype: str) -> None:
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj), "application/json")

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    cfg = load_cfg()
    print(
        f"http://{host}:{PORT}{BASE_PATH or '/'}"
        f" base_path={BASE_PATH or '(none)'} backend={cfg['backend'] or '(unset)'}",
        flush=True,
    )
    ThreadingHTTPServer((host, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
