#!/usr/bin/env python3
"""Ride list and incident list for the helmet-cam witness app.

Stdlib only. Reads app/incidents.stub.json until the engine writes incidents.json.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STUB = ROOT / "incidents.stub.json"
PORT = 8090

FAULT_LABEL = {
    "driver": "Driver",
    "rider": "Rider",
    "unclear": "Unclear",
    "shared": "Shared",
}


def load_data() -> dict:
    path = ROOT / "incidents.json"
    if not path.exists():
        path = STUB
    return json.loads(path.read_text())


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
    cards = []
    for ride in data["rides"]:
        mine = [item for item in incidents if item["ride"] == ride["id"]]
        found = counts(mine)
        unclear = f" · {found['unclear']} to decide" if found["unclear"] else ""
        cards.append(
            f"""<a class="card" href="/ride/{esc(ride['id'])}">
              <div class="row"><h2>{esc(ride['title'])}</h2><span class="when">{esc(ride['conditions'])}</span></div>
              <p class="summary">{esc(summary_line(found))}{esc(unclear)}</p>
              <p class="meta">{ride['duration_min']} min · {ride['segments']} segments · {len(mine)} flagged</p>
            </a>"""
        )
    body = f"""<h1>Rides</h1>
<p>{esc(summary_line(total))}. Open a ride to review what can be sent and what stays with you.</p>
<main class="rides">{''.join(cards)}</main>"""
    return page("Rides", body)


def incident_list(data: dict, ride_id: str, tab: str) -> bytes:
    ride = next((item for item in data["rides"] if item["id"] == ride_id), None)
    if ride is None:
        return page("Ride not found", "<h1>Ride not found</h1><p><a href='/'>All rides</a></p>")
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
            f"""<a class="card" href="/incident/{esc(item['id'])}">
              <div class="row">
                <h2>{esc(item['document']['title'])}</h2>
                <span class="badge {esc(fault)}">{esc(FAULT_LABEL.get(fault, fault))}</span>
              </div>
              <p>{esc(item['facts']['evidence'])}</p>
              <p class="when">{esc(item['id'])} · {esc(clock(item['ride_offset_sec']))} · {esc(item['severity_label'])} · {esc(colour + who)}</p>
            </a>"""
        )
    listing = "".join(rows) if rows else f'<p class="empty">{empty}</p>'
    body = f"""<p class="kicker"><a href="/">All rides</a></p>
<h1>{esc(ride['title'])}</h1>
<p class="summary">{esc(summary_line(found))}</p>
<p class="meta">{esc(ride['conditions'])}</p>
<nav class="tabs">
  <a class="{'on' if tab == 'reports' else ''}" href="/ride/{esc(ride_id)}?tab=reports">Reports to send</a>
  <a class="{'on' if tab == 'lessons' else ''}" href="/ride/{esc(ride_id)}?tab=lessons">Lessons for you</a>
</nav>
<main class="list">{listing}</main>"""
    return page(ride["title"], body)


def incident_detail(data: dict, incident_id: str) -> bytes:
    item = next((row for row in data["incidents"] if row["id"] == incident_id), None)
    if item is None:
        return page("Incident not found", "<h1>Incident not found</h1><p><a href='/'>All rides</a></p>")
    fault = item["fault"]
    body = f"""<p class="kicker"><a href="/ride/{esc(item['ride'])}">Back to {esc(item['ride'])}</a></p>
<h1>{esc(item['document']['title'])}</h1>
<p><span class="badge {esc(fault)}">{esc(FAULT_LABEL.get(fault, fault))}</span></p>
<p>{esc(item['facts']['evidence'])}</p>
<p class="when">{esc(item['source'])} · ride time {esc(clock(item['ride_offset_sec']))} · rule {esc(item['fault_rule'])}</p>
<p class="meta">Clip player and review actions are the next page. This stub confirms the incident is on the list.</p>"""
    return page(item["id"], body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        data = load_data()
        if path == "/":
            body = ride_list(data)
        elif path.startswith("/ride/"):
            ride_id = path.split("/", 2)[2]
            tab = (query.get("tab") or ["reports"])[0]
            body = incident_list(data, ride_id, tab)
        elif path.startswith("/incident/"):
            body = incident_detail(data, path.split("/", 2)[2])
        else:
            body = page("Not found", "<h1>Not found</h1><p><a href='/'>All rides</a></p>")
            self._send(body, 404)
            return
        self._send(body, 200)

    def _send(self, body: bytes, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"http://127.0.0.1:{PORT}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
