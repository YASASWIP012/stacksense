"""Tiny web server for the StackSense UI (Python standard library only)."""
import glob
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import kb, schema
from .engine import ENGINE_VERSION, recommend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
SCEN = os.path.join(ROOT, "scenarios")
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


def samples():
    out = []
    for path in sorted(glob.glob(os.path.join(SCEN, "public", "P*.json"))) + \
            sorted(glob.glob(os.path.join(SCEN, "edge", "*.json"))):
        name = os.path.splitext(os.path.basename(path))[0]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        title = (data.get("project") or {}).get("name") if isinstance(data.get("project"), dict) else None
        out.append({"id": name, "group": "public" if "public" in path else "edge", "title": title or name})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "StackSense/" + ENGINE_VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/schema":
            return self._send(200, {"fields": schema.schema_for_ui(), "engine_version": ENGINE_VERSION,
                                    "kb": kb.kb_stats(), "llm_used": False})
        if u.path == "/api/samples":
            return self._send(200, samples())
        if u.path == "/api/sample":
            name = parse_qs(u.query).get("id", [""])[0]
            for grp in ("public", "edge"):
                p = os.path.join(SCEN, grp, os.path.basename(name) + ".json")
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as f:
                        return self._send(200, f.read())
            return self._send(404, {"error": "unknown sample"})
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(WEB) or not os.path.isfile(path):
            return self._send(404, {"error": "not found"})
        with open(path, "rb") as f:
            return self._send(200, f.read(), TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))

    def do_POST(self):
        if urlparse(self.path).path != "/api/recommend":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(n, 2_000_000))
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError as e:
            return self._send(200, {"status": "invalid_input", "summary": "Body is not valid JSON.", "stack": None,
                                    "warnings": [], "assumptions": [],
                                    "validation_errors": [{"field": "$", "message": str(e)}]})
        return self._send(200, recommend(data))


def serve(host="127.0.0.1", port=8000):
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print("Could not start on %s:%d (%s). Try another port: python3 -m stacksense serve --port 8080" % (host, port, e))
        return
    print("StackSense %s running at http://%s:%d  (Ctrl+C to stop)" % (ENGINE_VERSION, host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
