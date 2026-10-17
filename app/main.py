"""Rider safety agent web app (stdlib only).

Served at / inside the pod; the Ingress maps https://<team host>/app/... to /...
The VSS JWT stays server-side: the browser only ever talks to this app.
"""
import json
import os
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL", "http://video-backend-service:8000").rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", "")
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", "")
HERE = Path(__file__).resolve().parent

ALLOWED_SOURCE_PREFIXES = tuple(
    f"s3://{VSS_USERNAME}-{b}/" for b in ("vss-chunks-segments", "vss-chunks")
)
PASS_THROUGH_HEADERS = (
    "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Cache-Control",
)

_token = None
_token_lock = threading.Lock()


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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self._json(200, {"ok": True})
        if url.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/stream":
            return self.stream(qs.get("source", [""])[0])
        return self._send(404, "not found")

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


if __name__ == "__main__":
    print(f"listening on 0.0.0.0:{PORT}, backend {VSS_URL}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
