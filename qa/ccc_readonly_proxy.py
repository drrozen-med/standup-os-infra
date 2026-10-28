"""Read-only stand-in for CCC, for QA runs of the navigator.

GET/HEAD are forwarded to the real CCC (streams included). Every write
(POST/PUT/PATCH/DELETE) is answered with a fake success and logged, so QA can
press Send, Rename, Stop, etc. without touching a real agent session.
"""
import http.server
import json
import socketserver
import sys
import time
import urllib.error
import urllib.request

UPSTREAM = "http://localhost:8090"
LOG = sys.argv[2] if len(sys.argv) > 2 else "blocked-writes.log"


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self, head=False):
        req = urllib.request.Request(UPSTREAM + self.path, method="HEAD" if head else "GET",
                                     headers={k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")})
        try:
            up = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            up = e
        except Exception as e:  # upstream down
            body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(up.status if hasattr(up, "status") else up.code)
        for k, v in up.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if head:
            return
        try:
            while True:
                chunk = up.read1(65536) if hasattr(up, "read1") else up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        self._forward()

    def do_HEAD(self):
        self._forward(head=True)

    def _blocked(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with open(LOG, "a") as fh:
            fh.write("%s %s %s %s\n" % (time.strftime("%H:%M:%S"), self.command, self.path, body[:300].decode("utf-8", "replace")))
        out = json.dumps({"ok": True, "sandbox": "write blocked by QA proxy"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_POST = do_PUT = do_PATCH = do_DELETE = _blocked

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    S(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8097), H).serve_forever()
