#!/usr/bin/env python3
"""Rider safety agent web app (stdlib only).

Served at / inside the pod; the Ingress maps https://<team host>/app/... to /..., so every link is
built from BASE (the public prefix). The VSS JWT stays server-side: the browser only talks to this app.

Pages: rides -> ride (Reports to send / Lessons for you) -> incident (clip + facts), plus the Stage A
screening timeline. Incidents come from incidents.json (engine output) or app/incidents.stub.json.
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

PORT = int(os.environ.get("PORT", "8080"))
_PREFIX = os.environ.get("BASE_PATH", "").strip("/")
BASE = f"/{_PREFIX}/" if _PREFIX else "/"
VSS_URL = os.environ.get("VSS_URL", "http://video-backend-service:8000").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
HERE = Path(__file__).resolve().parent
STUB = HERE / "incidents.stub.json"
# Data lives in its own ConfigMap mounted at /data, re-read on every request, so the engine's output
# can be swapped with k8s/update-data.sh without touching or restarting the app.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
if not DATA_DIR.is_dir():
    DATA_DIR = HERE.parent / "data"

ALLOWED_SOURCE_PREFIXES = tuple(
    f"s3://{VSS_USERNAME}-{b}/" for b in ("vss-chunks-segments", "vss-chunks")
)
PASS_THROUGH_HEADERS = (
    "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control",
)
SEGMENT_KEY = re.compile(r"((?:GOPR|GX)\d+_chunk_\d+_segment_\d+)")

FAULT_LABEL = {
    "driver": "Driver",
    "rider": "Rider",
    "unclear": "Unclear",
    "shared": "Shared",
}

RULE_TEXT = {
    3: "Rule 3: the signal facing the rider was red",
    4: "Rule 4: the rider was on the sidewalk",
    5: "Rule 5: the rider changed line into the vehicle's path",
    6: "Rule 6: the rider was riding in the door zone",
    7: "Rule 7: the vehicle moved into the rider's path, with nothing pointing to the rider",
    8: "Rule 8: the boxes and the model disagree about how close it was",
    9: "Rule 9: no signal visible on a crossing or turn, so the rider decides",
}

_token = None
_token_lock = threading.Lock()


# --- VSS backend -------------------------------------------------------------------------------

def _login():
    body = json.dumps({"username": VSS_USERNAME, "password": VSS_PASSWORD}).encode()
    req = urllib.request.Request(
        VSS_URL + "/api/v1/auth/login", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def get_token(refresh=False):
    global _token
    with _token_lock:
        if refresh or _token is None:
            _token = _login()
        return _token


def vss_open(path, params, headers=None, timeout=60):
    """GET a backend path with the JWT as ?token= (what /videos/stream expects); re-login once on 401/403."""
    for attempt in (0, 1):
        q = urllib.parse.urlencode({**params, "token": get_token(refresh=attempt == 1)})
        req = urllib.request.Request(f"{VSS_URL}{path}?{q}", headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                continue
            raise


# --- Data --------------------------------------------------------------------------------------

def read_data(name, default):
    try:
        return json.loads((DATA_DIR / name).read_text())
    except FileNotFoundError:
        return default


def load_data() -> dict:
    data = read_data("incidents.json", None)
    return data if data is not None else json.loads(STUB.read_text())


def segment_sources() -> dict[str, str]:
    """'GOPR0130_chunk_0026_segment_003' -> playable s3:// segment URI."""
    out = {}
    for s in read_data("segments.json", []):
        m = SEGMENT_KEY.search(s["source"])
        if m:
            out[m.group(1)] = s["source"]
    return out


def resolve_source(source: str) -> str | None:
    if source.startswith("s3://"):
        return source
    m = SEGMENT_KEY.search(source)
    return segment_sources().get(m.group(1)) if m else None


def load_rides():
    """Segments (playable sources) joined with Stage A screening rows on (chunk file, start second)."""
    screen = read_data("screen.json", {})
    by_key = {(r["clip"], float(r["start_sec"])): r for r in screen.get("rows", [])}
    rides = {}
    for s in read_data("segments.json", []):
        r = by_key.get((s["clip"], s["start_sec"]))
        seg = dict(s, screened=r is not None)
        if r:
            seg.update(screen=r.get("screen"), label=r.get("label"), fact=r.get("fact"),
                       red=r.get("red"), screen_id=r.get("id"))
        rides.setdefault(s["ride"], []).append(seg)
    return rides, screen


def ride_summaries(rides, screen):
    out = []
    for ride, segs in sorted(rides.items()):
        counts = {}
        for s in segs:
            if s["screened"]:
                counts[s["screen"]] = counts.get(s["screen"], 0) + 1
        out.append({
            "ride": ride,
            "segments": len(segs),
            "screened": sum(s["screened"] for s in segs),
            "duration_sec": max(s["offset_sec"] + s["end_sec"] - s["start_sec"] for s in segs),
            "counts": counts,
        })
    return {
        "rides": out,
        "legend": screen.get("legend", {}),
        "stage": screen.get("stage"),
        "partial": screen.get("partial"),
    }


# --- Pages -------------------------------------------------------------------------------------

def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def plain(value: object) -> str:
    """Engine enum values for people: none_visible -> none visible, True -> yes."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).replace("_", " ")


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


def page(title: str, body: str) -> bytes:
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
  header, main {{ width: min(920px, calc(100% - 32px)); margin: 0 auto; }}
  header {{ padding: 28px 0 8px; }}
  header p, .meta {{ color: var(--muted); }}
  h1 {{ font-size: 28px; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 18px; font-weight: 600; margin: 0; }}
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
  .tabs {{ display: flex; gap: 8px; margin: 18px 0; }}
  .tabs a {{
    font: 14px/1 ui-sans-serif, system-ui, sans-serif;
    text-decoration: none;
    border: 1px solid var(--line);
    padding: 8px 12px;
    background: transparent;
  }}
  .tabs a.on {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}
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
  .strip {{ position: relative; height: 22px; background: #e7e1d6; margin: 14px 0 4px; }}
  .strip a {{ position: absolute; top: 0; bottom: 0; width: 6px; margin-left: -3px; }}
  .strip .driver {{ background: var(--driver); }}
  .strip .rider {{ background: var(--rider); }}
  .strip .unclear, .strip .shared {{ background: var(--unclear); }}
  video {{ width: 100%; background: #000; margin: 12px 0; }}
  .facts {{ display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; font: 14px/1.4 ui-sans-serif, system-ui, sans-serif; }}
  .facts dt {{ color: var(--muted); }}
  .facts dd {{ margin: 0; }}
</style>
</head>
<body>
<header>
  <p class="kicker">Helmet cam witness</p>
  {body}
</header>
</body>
</html>"""
    return html.encode()


def ride_list(data: dict) -> bytes:
    incidents = data["incidents"]
    total = counts(incidents)
    screening = {r["ride"]: r for r in ride_summaries(*load_rides())["rides"]}
    cards = []
    for ride in data["rides"]:
        mine = [item for item in incidents if item["ride"] == ride["id"]]
        found = counts(mine)
        unclear = f" · {found['unclear']} to decide" if found["unclear"] else ""
        scr = screening.get(ride["id"])
        screened = f" · {scr['screened']} screened by Cosmos" if scr and scr["screened"] else ""
        cards.append(
            f"""<a class="card" href="{BASE}ride/{esc(ride['id'])}">
              <div class="row"><h2>{esc(ride['title'])}</h2><span class="when">{esc(ride['conditions'])}</span></div>
              <p class="summary">{esc(summary_line(found))}{esc(unclear)}</p>
              <p class="meta">{ride['duration_min']} min · {ride['segments']} segments{esc(screened)} · {len(mine)} flagged</p>
            </a>"""
        )
    body = f"""<h1>Rides</h1>
<p>{esc(summary_line(total))}. Open a ride to review what can be sent and what stays with you.</p>
<main class="rides">{''.join(cards)}</main>"""
    return page("Rides", body)


def incident_strip(ride: dict, mine: list[dict]) -> str:
    """Should 3: one mark per incident along the ride, coloured by fault."""
    length = ride["duration_min"] * 60 or 1
    marks = "".join(
        f'<a class="{esc(item["fault"])}" href="{BASE}incident/{esc(item["id"])}" '
        f'style="left:{min(100, 100 * item["ride_offset_sec"] / length):.2f}%" '
        f'title="{esc(item["id"])} {esc(clock(item["ride_offset_sec"]))}"></a>'
        for item in mine
    )
    return f"""<div class="strip">{marks}</div>
<p class="when">0:00 to {ride['duration_min']} min · <a href="{BASE}timeline#ride={esc(ride['id'])}">Screening timeline (every segment)</a></p>"""


def incident_list(data: dict, ride_id: str, tab: str) -> bytes:
    ride = next((item for item in data["rides"] if item["id"] == ride_id), None)
    if ride is None:
        return page("Ride not found", f"<h1>Ride not found</h1><p><a href='{BASE}'>All rides</a></p>")
    mine = [item for item in data["incidents"] if item["ride"] == ride_id]
    mine.sort(key=lambda item: item.get("severity") or 0, reverse=True)
    found = counts(mine)
    if tab not in ("reports", "lessons"):
        tab = "reports"
    if tab == "reports":
        shown = [item for item in mine if item["fault"] in ("driver", "unclear", "shared")]
        empty = "No reports to send on this ride."
    else:
        shown = [item for item in mine if item["fault"] in ("rider", "unclear", "shared")]
        empty = "No lessons on this ride."
    rows = []
    for item in shown:
        fault = item["fault"]
        vehicle = item["facts"]["vehicle"]
        kind = vehicle["type"]
        who = "no vehicle" if kind == "none" else {"suv": "SUV"}.get(kind, kind)
        colour = f"{vehicle['colour']} " if vehicle.get("colour") else ""
        rows.append(
            f"""<a class="card" href="{BASE}incident/{esc(item['id'])}">
              <div class="row">
                <h2>{esc(item['document']['title'])}</h2>
                <span class="badge {esc(fault)}">{esc(FAULT_LABEL.get(fault, fault))}</span>
              </div>
              <p>{esc(item['facts']['evidence'])}</p>
              <p class="when">{esc(item['id'])} · {esc(clock(item['ride_offset_sec']))} · {esc(item['severity_label'])} · {esc(colour + who)}</p>
            </a>"""
        )
    listing = "".join(rows) if rows else f'<p class="empty">{empty}</p>'
    body = f"""<p class="kicker"><a href="{BASE}">All rides</a></p>
<h1>{esc(ride['title'])}</h1>
<p class="summary">{esc(summary_line(found))}</p>
<p class="meta">{esc(ride['conditions'])}</p>
{incident_strip(ride, mine)}
<nav class="tabs">
  <a class="{'on' if tab == 'reports' else ''}" href="{BASE}ride/{esc(ride_id)}?tab=reports">Reports to send</a>
  <a class="{'on' if tab == 'lessons' else ''}" href="{BASE}ride/{esc(ride_id)}?tab=lessons">Lessons for you</a>
</nav>
<main class="list">{listing}</main>"""
    return page(ride["title"], body)


def incident_detail(data: dict, incident_id: str) -> bytes:
    item = next((row for row in data["incidents"] if row["id"] == incident_id), None)
    if item is None:
        return page("Incident not found", f"<h1>Incident not found</h1><p><a href='{BASE}'>All rides</a></p>")
    fault = item["fault"]
    facts = item["facts"]
    vehicle = facts.get("vehicle") or {}
    src = resolve_source(item["source"])
    start = max(0.0, float(item.get("t_peak") or 0) - 1.5)
    player = (
        f'<video controls preload="metadata" '
        f'src="{BASE}api/stream?source={esc(urllib.parse.quote(src, safe=""))}#t={start:.1f}"></video>'
        if src else '<p class="empty">Clip not found in the segment index.</p>'
    )
    side = vehicle.get("side")
    where = {"front": "in front", "none": None}.get(side, side and f"on the {side}")
    kind = {"suv": "SUV"}.get(vehicle.get("type"), vehicle.get("type"))
    rule = item.get("fault_rule")
    rows = [
        ("Signal facing rider", facts.get("signal_for_rider")),
        ("Rider position", facts.get("rider_position")),
        ("Physical separator", facts.get("separator")),
        ("Vehicle", " ".join(str(v) for v in (vehicle.get("colour"), kind, where) if v)),
        ("Vehicle action", facts.get("vehicle_action")),
        ("Vehicle entered rider's path", facts.get("vehicle_entered_rider_path")),
        ("Rider moved into vehicle's path", facts.get("rider_moved_into_vehicle_path")),
        ("Closest distance", {"under_1m": "under 1 m", "1_to_2m": "1 to 2 m", "over_2m": "over 2 m"}.get(facts.get("closest_distance"), facts.get("closest_distance"))),
        ("Cosmos's own fault call (shown, not used)", facts.get("model_fault")),
        ("Why this verdict", RULE_TEXT.get(rule, rule and f"Rule {rule}")),
    ]
    fact_list = "".join(f"<dt>{esc(k)}</dt><dd>{esc(plain(v))}</dd>" for k, v in rows if v not in (None, ""))
    body = f"""<p class="kicker"><a href="{BASE}ride/{esc(item['ride'])}">Back to {esc(item['ride'])}</a></p>
<h1>{esc(item['document']['title'])}</h1>
<p><span class="badge {esc(fault)}">{esc(FAULT_LABEL.get(fault, fault))}</span></p>
<p>{esc(facts['evidence'])}</p>
<p class="when">{esc(item['source'])} · ride time {esc(clock(item['ride_offset_sec']))} · peak at {esc(item.get('t_peak'))} s</p>
{player}
<dl class="facts">{fact_list}</dl>
<p class="meta">Review actions and the report / lesson come next.</p>"""
    return page(item["id"], body)


# --- HTTP --------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _html(self, code, body):
        self._send(code, body, "text/html; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        if path == "/health":
            return self._json(200, {"ok": True})
        if path == "/api/stream":
            return self.stream(qs.get("source", [""])[0])
        if path == "/api/rides":
            return self._json(200, ride_summaries(*load_rides()))
        if path.startswith("/api/rides/"):
            rides, screen = load_rides()
            ride = urllib.parse.unquote(path[len("/api/rides/"):])
            if ride not in rides:
                return self._json(404, {"error": f"unknown ride {ride}"})
            return self._json(200, {"ride": ride, "legend": screen.get("legend", {}), "segments": rides[ride]})
        if path == "/api/incidents":
            return self._json(200, load_data())
        if path == "/timeline":
            html = (HERE / "timeline.html").read_text().replace("__BASE__", BASE)
            return self._html(200, html)
        data = load_data()
        if path == "/":
            return self._html(200, ride_list(data))
        if path.startswith("/ride/"):
            tab = (qs.get("tab") or ["reports"])[0]
            return self._html(200, incident_list(data, urllib.parse.unquote(path.split("/", 2)[2]), tab))
        if path.startswith("/incident/"):
            return self._html(200, incident_detail(data, urllib.parse.unquote(path.split("/", 2)[2])))
        return self._html(404, page("Not found", f"<h1>Not found</h1><p><a href='{BASE}'>All rides</a></p>"))

    def stream(self, source):
        """Proxy a segment clip, forwarding Range so <video> can seek (206 Partial Content)."""
        if not source.startswith(ALLOWED_SOURCE_PREFIXES):
            return self._json(400, {"error": "source must be an s3:// URI in this team's chunk buckets"})
        fwd = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            upstream = vss_open("/api/v1/videos/stream", {"source": source}, fwd)
        except urllib.error.HTTPError as e:
            return self._json(e.code, {"error": f"backend returned {e.code}"})
        except (urllib.error.URLError, TimeoutError) as e:
            return self._json(502, {"error": f"backend unreachable: {e}"})
        with upstream:
            self.send_response(upstream.status)
            self.send_header("Content-Type", "video/mp4" if source.endswith(".mp4") else
                             upstream.headers.get("Content-Type", "application/octet-stream"))
            for h in PASS_THROUGH_HEADERS:
                if upstream.headers.get(h):
                    self.send_header(h, upstream.headers[h])
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
                pass  # browser cancelled the range request while seeking


def main() -> None:
    print(f"listening on 0.0.0.0:{PORT}, base {BASE}, backend {VSS_URL}, data {DATA_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
