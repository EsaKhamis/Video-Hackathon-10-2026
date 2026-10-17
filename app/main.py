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
PORT = int(os.environ.get("PORT", "8090"))
# In the pod the code is a read-only ConfigMap mount: reviews go to a writable path, seeded from app/reviews.json.
REVIEWS = Path(os.environ.get("REVIEWS_PATH", str(ROOT / "reviews.json")))
# Engine output lives in its own ConfigMap at /data (k8s/update-data.sh), re-read on every request.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
if not DATA_DIR.is_dir():
    DATA_DIR = ROOT.parent / "data"
SEGMENT_KEY = re.compile(r"((?:GOPR|GX)\d+_chunk_\d+_segment_\d+)")
# Public URL prefix when behind Ingress path /app (rewrite strips it before the app sees the path).
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")


def u(path: str = "/") -> str:
    """Client-facing path, prefixed with BASE_PATH when deployed at /app."""
    if not path.startswith("/"):
        path = "/" + path
    return f"{BASE_PATH}{path}" if BASE_PATH else path


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


def load_data() -> dict:
    path = DATA_DIR / "incidents.json"
    if not path.exists():
        path = ROOT / "incidents.json"
    if not path.exists():
        path = STUB
    data = json.loads(path.read_text())
    for item in data.get("incidents") or []:
        if not item.get("preview_source"):
            item["preview_source"] = resolve_source(item.get("source", ""))
    reviews = load_reviews()
    for item in data.get("incidents") or []:
        override = reviews.get(item["id"])
        if override:
            item["review"] = {**item.get("review", {}), **override}
            if override.get("fault_override"):
                item["fault"] = override["fault_override"]
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


def page(title: str, body: str, extra_head: str = "") -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{
    color-scheme: light;
    --ink: #1c1915;
    --muted: #5c564c;
    --line: #d9d2c5;
    --paper: #f6f3ee;
    --card: #fffdf9;
    --driver: #8c2f2f;
    --rider: #8a5a12;
    --unclear: #3f4a46;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font: 16px/1.45 Georgia, "Iowan Old Style", serif;
  }}
  a {{ color: inherit; }}
  header, main {{ width: min(960px, calc(100% - 32px)); margin: 0 auto; }}
  header {{ padding: 28px 0 8px; }}
  header p, .meta {{ color: var(--muted); }}
  h1 {{ font-size: 28px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 18px; font-weight: 600; margin: 0; }}
  h3 {{ font: 13px/1.2 ui-sans-serif, system-ui, sans-serif; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); margin: 0 0 10px; }}
  .kicker {{
    font: 12px/1.2 ui-sans-serif, system-ui, sans-serif;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 8px;
  }}
  .rides, .list {{ display: grid; gap: 12px; margin: 18px 0 40px; }}
  .card {{
    display: block;
    background: var(--card);
    border: 1px solid var(--line);
    padding: 16px 18px;
    text-decoration: none;
  }}
  .card:hover {{ border-color: var(--ink); }}
  .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: baseline; }}
  .summary {{ font-size: 20px; margin: 8px 0 0; }}
  .tabs {{ display: flex; gap: 8px; margin: 18px 0; flex-wrap: wrap; }}
  .tabs a, .tabs button {{
    font: 14px/1 ui-sans-serif, system-ui, sans-serif;
    text-decoration: none;
    border: 1px solid var(--line);
    padding: 8px 12px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }}
  .tabs a.on, .tabs button.on {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}
  .badge {{
    font: 12px/1 ui-sans-serif, system-ui, sans-serif;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 4px 7px;
    color: white;
  }}
  .badge.driver {{ background: var(--driver); }}
  .badge.rider {{ background: var(--rider); }}
  .badge.unclear, .badge.shared {{ background: var(--unclear); }}
  .when {{ font: 13px/1.3 ui-sans-serif, system-ui, sans-serif; color: var(--muted); }}
  .empty {{ color: var(--muted); }}
  .stage {{ display: grid; gap: 16px; margin: 18px 0 40px; }}
  @media (min-width: 860px) {{
    .stage {{ grid-template-columns: 1.4fr 1fr; align-items: start; }}
  }}
  .panel {{ background: var(--card); border: 1px solid var(--line); padding: 16px; }}
  .player {{ position: relative; background: #111; aspect-ratio: 16/9; }}
  .player video, .player canvas {{
    position: absolute; inset: 0; width: 100%; height: 100%;
  }}
  .player canvas {{ pointer-events: none; }}
  .story {{ margin: 0 0 14px; font-size: 17px; line-height: 1.5; }}
  details.tech {{ margin-top: 4px; }}
  details.tech summary {{
    font: 13px/1.2 ui-sans-serif, system-ui, sans-serif;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
    cursor: pointer;
  }}
  .facts {{ display: grid; gap: 8px; font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; margin-top: 10px; }}
  .facts div {{ display: grid; grid-template-columns: 9.5rem 1fr; gap: 8px; }}
  .facts dt {{ color: var(--muted); }}
  .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
  .actions button, .actions a.button {{
    font: 14px/1 ui-sans-serif, system-ui, sans-serif;
    border: 1px solid var(--ink);
    background: transparent;
    color: var(--ink);
    padding: 10px 12px;
    cursor: pointer;
    text-decoration: none;
  }}
  .actions button.on, .actions a.button.on {{
    background: var(--ink);
    color: var(--paper);
  }}
  .actions button.secondary, .actions a.button.secondary {{
    background: transparent;
    color: var(--ink);
  }}
  .hint {{ font: 13px/1.4 ui-sans-serif, system-ui, sans-serif; color: var(--muted); margin: 0 0 10px; }}
  .audience {{
    font: 15px/1.45 Georgia, "Iowan Old Style", serif;
    margin: 0 0 14px;
    padding: 12px 14px;
    border: 1px solid var(--line);
    background: var(--paper);
  }}
  label {{ display: grid; gap: 4px; font: 13px/1.3 ui-sans-serif, system-ui, sans-serif; margin-top: 10px; }}
  input, select {{
    font: 14px/1.3 ui-sans-serif, system-ui, sans-serif;
    padding: 8px 10px;
    border: 1px solid var(--line);
    background: white;
  }}
  .status {{ font: 13px/1.3 ui-sans-serif, system-ui, sans-serif; color: var(--muted); margin-top: 10px; }}
  .agree {{ font: 13px/1.3 ui-sans-serif, system-ui, sans-serif; margin-top: 8px; }}
  .agree.yes {{ color: #2f5d3a; }}
  .agree.no {{ color: var(--driver); }}
  .timeline {{ position: relative; height: 28px; margin: 16px 0 6px; }}
  .timeline .track {{
    position: absolute; left: 0; right: 0; top: 12px; height: 4px;
    background: var(--line);
  }}
  .timeline .mark {{
    position: absolute; top: 5px; width: 16px; height: 16px; margin-left: -8px;
    border-radius: 50%; border: 2px solid var(--paper);
  }}
  .timeline .mark.driver {{ background: var(--driver); }}
  .timeline .mark.rider {{ background: var(--rider); }}
  .timeline .mark.unclear, .timeline .mark.shared {{ background: var(--unclear); }}
  .navrow {{ display: flex; justify-content: space-between; gap: 12px; margin: 12px 0 0; font: 13px/1.3 ui-sans-serif, system-ui, sans-serif; }}
</style>
{extra_head}
</head>
<body>
<header>
  <p class="kicker">Helmet cam witness</p>
  {body}
</header>
</body>
</html>"""
    return html.encode()


def ride_label(ride: dict) -> str:
    return ride.get("title") or ride.get("id") or "Ride"


def ride_list(data: dict) -> bytes:
    incidents = data["incidents"]
    total = counts(incidents)
    cards = []
    for ride in data["rides"]:
        mine = [item for item in incidents if item["ride"] == ride["id"]]
        found = counts(mine)
        unclear = f" · {found['unclear']} to decide" if found["unclear"] else ""
        situation = ride.get("situation") or ride.get("conditions") or ""
        cards.append(
            f"""<a class="card" href="{u('/ride/' + ride['id'])}">
              <div class="row"><h2>{esc(ride_label(ride))}</h2><span class="when">{esc(clock((ride.get('duration_min') or 0) * 60))} ride</span></div>
              <p class="summary">{esc(summary_line(found))}{esc(unclear)}</p>
              <p class="meta">{esc(situation)}</p>
            </a>"""
        )
    body = f"""<h1>Your rides</h1>
<p id="page-audience">For you, the cyclist. Open a ride to review risky moments: reports you may send, and private lessons that never leave this app.</p>
<p class="meta">{esc(summary_line(total))} across these rides.</p>
<main class="rides">{''.join(cards)}</main>
<p class="meta"><a href="{u('/timeline')}">How the agent screened every 5-second segment</a></p>"""
    return page("Your rides", body)


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
    body = f"""<p class="kicker"><a href="{u('/')}">Your rides</a></p>
<h1>{esc(ride_label(ride))}</h1>
<p class="summary">{esc(summary_line(found))}</p>
<p class="meta">{esc(situation)}</p>
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
    elif fault == "rider":
        parts.append("The rules treat this as a rider lesson, not a report to send.")
    elif fault == "unclear":
        parts.append("The boxes and the model disagree, so a person should decide.")
    elif fault == "shared":
        parts.append("Both sides look involved; the reviewer can choose the final call.")

    return " ".join(p for p in parts if p)


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
        ("Vehicle", f"{vehicle.get('colour', '')} {vehicle.get('type', '')} · {vehicle.get('side', '')}".strip()),
        ("Closest distance", facts.get("closest_distance")),
        ("Peak height", f"{(geom.get('peak_h') or 0) * 100:.0f}% of frame"),
        ("Entered corridor", yn(geom.get("entered_corridor"))),
        ("Rule", f"#{item.get('fault_rule')} → {FAULT_LABEL.get(rule_fault, rule_fault)}"),
        ("Model fault", FAULT_LABEL.get(model_fault, model_fault)),
    ]
    facts_html = "".join(
        f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>" for label, value in fact_rows
    )
    story = story_from_facts(item)
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

    body = f"""<p class="kicker"><a href="{u('/ride/' + item['ride'])}">Back to {esc(ride_label(ride))}</a></p>
<h1>{esc(item['document']['title'])}</h1>
<p><span class="badge {esc(fault)}" id="fault-badge">{esc(FAULT_LABEL.get(fault, fault))}</span>
<span class="when"> · {esc(item['severity_label'])} · {esc(clock(item['ride_offset_sec']))} into the ride · peak {esc(item.get('t_peak'))}s</span></p>
<p>{esc(facts['evidence'])}</p>
<p class="agree {agree_cls}">{esc(agree_txt)}</p>
<nav class="navrow">{prev_link}{next_link}</nav>

<main class="stage">
  <section class="panel">
    <h3>Clip</h3>
    <div class="player">
      <video id="v" controls playsinline preload="metadata"></video>
      <canvas id="c"></canvas>
    </div>
    <p class="status" id="play-status">Loading clip…</p>
  </section>
  <section class="panel">
    <h3>What happened</h3>
    <p class="story">{esc(story)}</p>
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
const source = {json.dumps(source)};
const bbox = {bbox_json};
const peak = {json.dumps(item.get("t_peak") or 0)};
const v = document.getElementById('v');
const c = document.getElementById('c');
const status = document.getElementById('play-status');
const reviewStatus = document.getElementById('review-status');
let chosenFault = {json.dumps(fault)};

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
  if (!bbox) return;
  const [x1, y1, x2, y2] = bbox;
  ctx.strokeStyle = '#f2c14e';
  ctx.lineWidth = 3 * devicePixelRatio;
  ctx.strokeRect(x1 * c.width, y1 * c.height, (x2 - x1) * c.width, (y2 - y1) * c.height);
}}

if (source) {{
  v.src = basePath + '/api/stream?source=' + encodeURIComponent(source);
  v.addEventListener('loadedmetadata', () => {{
    status.textContent = `Loaded ${{v.duration.toFixed(1)}} s. Peak at ${{peak}} s.`;
    sizeCanvas();
    try {{ v.currentTime = Math.min(peak, Math.max(0, v.duration - 0.1)); }} catch (e) {{}}
  }});
  v.addEventListener('seeked', () => {{ sizeCanvas(); status.textContent = `At ${{v.currentTime.toFixed(1)}} s`; }});
  v.addEventListener('play', sizeCanvas);
  v.addEventListener('error', () => {{ status.textContent = 'Playback error — check /api/stream.'; }});
  window.addEventListener('resize', sizeCanvas);
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
    why = "Close conflict with traffic raises the chance of a crash."
    if pos == "sidewalk":
        why = "Riding on the sidewalk surprises pedestrians and forces sudden moves into the road."
    elif signal == "red":
        why = "Entering while the signal facing you is red puts you into the path of crossing traffic."
    elif facts.get("vehicle_entered_rider_path"):
        why = "A vehicle entering your line leaves little room to brake or swerve."
    next_time = "Hold a predictable line, scan early, and give yourself an escape gap."
    if pos == "sidewalk":
        next_time = "Stay on the carriageway or bike lane when you can; if you must leave it, slow for pedestrians and rejoin carefully."
    elif signal == "red":
        next_time = "Stop for the red facing you. Wait for green or a clear gap before entering the intersection."
    elif facts.get("vehicle_action") == "overtaking":
        next_time = "Hold your line, glance right, and leave space to move left if a pass is too close."
    return {"what_happened": story, "why_risky": why, "next_time": next_time}


def report_summary(item: dict) -> str:
    doc = item.get("document") or {}
    if doc.get("summary"):
        return str(doc["summary"])
    return story_from_facts(item)


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
    kind = {"suv": "SUV"}.get(kind, kind)
    vehicle_line = f"{colour} {kind}".strip() if colour else kind
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
    <p class="kicker">Authority report · audience: police / 311 / insurer (only if you export)</p>
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
      <div><dt>Event type</dt><dd>{esc(item.get('event_type'))}</dd></div>
      <div><dt>Severity</dt><dd>{esc(item.get('severity_label'))} ({esc(item.get('severity'))})</dd></div>
      <div><dt>Fault</dt><dd>{esc(FAULT_LABEL.get(fault, fault))}</dd></div>
      <div><dt>Signal for rider</dt><dd>{esc(facts.get('signal_for_rider'))}</dd></div>
      <div><dt>Rider position</dt><dd>{esc(facts.get('rider_position'))}</dd></div>
      <div><dt>Clip</dt><dd>{esc(item.get('source'))}</dd></div>
    </div>
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
    <p class="kicker">Rider lesson · audience: you only · never sent</p>
    <h1>{esc((item.get('document') or {}).get('title') or 'Rider lesson')}</h1>
    <p class="audience">This stays with you. There is no send button and it is not a police report.</p>
    <h2>What happened</h2>
    <p>{esc(sections['what_happened'])}</p>
    <h2>Why it was risky</h2>
    <p>{esc(sections['why_risky'])}</p>
    <h2>Next time</h2>
    <p>{esc(sections['next_time'])}</p>
    <p class="meta">General safety advice only — not legal advice, and not a claim about a fine or offence.</p>
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
            return self._json(200, {"ok": True})
        if path == "/api/incidents":
            return self._json(200, load_data())
        if path == "/api/stream":
            return self.stream(query.get("source", [""])[0])
        if path == "/api/detections":
            return self.detections(query.get("source", [""])[0])
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
            {"path": path, "base_path": BASE_PATH, "incidents": len(data.get("incidents") or [])},
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
