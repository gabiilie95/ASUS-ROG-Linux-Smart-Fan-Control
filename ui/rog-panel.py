#!/usr/bin/env python3
"""Local settings panel for the ROG G18 tuning project. Standard library only, 127.0.0.1 only."""
import argparse, errno, json, os, secrets, subprocess, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from rogcore import UI, _cache, busy, build_action, execute, execute_all, preset_action, status

class Handler(BaseHTTPRequestHandler):
    server_version = "rog-panel"
    token = ""
    port = 8765
    dry_run = False

    def log_message(self, *a):
        pass

    def send(self, code, data, ctype="application/json"):
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get("Host") in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(UI, "index.html"), "rb") as f:
                html = f.read().replace(b"__TOKEN__", self.token.encode())
            return self.send(200, html, "text/html; charset=utf-8")
        if path == "/api/status":
            return self.send(200, dict(status(), dry_run=self.dry_run))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        if not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), self.token):
            return self.send(403, {"error": "missing or wrong token"})
        name = self.path.split("?")[0].removeprefix("/api/")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            if name in ("save-preset", "delete-preset"):
                argv, timeout, note = None, 0, None
                if not busy.acquire(blocking=False):
                    return self.send(409, {"ok": False, "error": "another action is running"})
                try:
                    return self.send(200, preset_action(name, body))
                except ValueError as e:
                    return self.send(400, {"ok": False, "error": str(e)})
                except OSError as e:
                    return self.send(500, {"ok": False, "error": "could not write presets: %s" % e})
                finally:
                    busy.release()
            argv, timeout, note = build_action(name, body)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send(400, {"ok": False, "error": str(e)})
        if not busy.acquire(blocking=False):
            return self.send(409, {"ok": False, "error": "another action is running"})
        try:
            multi = bool(argv) and isinstance(argv[0], list)
            if self.dry_run:
                lines = [" ".join(a) for a in (argv if multi else [argv])]
                res = {"ok": True, "dry_run": True, "command": argv,
                       "output": "dry run, not executed:\n" + "\n".join(lines)}
            else:
                res = execute_all(argv, timeout) if multi else execute(name, argv, timeout)
                _cache.clear()      # show the new state on the next poll
            if note:
                res["note"] = note
            self.send(200, res)
        finally:
            busy.release()

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open the page with xdg-open")
    ap.add_argument("--dry-run", action="store_true", help="return commands instead of running them")
    a = ap.parse_args()
    Handler.token = secrets.token_urlsafe(32)
    Handler.port = a.port
    Handler.dry_run = a.dry_run
    url = "http://127.0.0.1:%d/" % a.port

    def open_browser():
        if a.no_browser:
            return
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print("could not open browser:", e)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        # If our own panel already holds the port, just show it instead of failing.
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                ours = b"<title>ROG panel</title>" in r.read(4096)
        except Exception:
            ours = False
        if ours:
            print("ROG panel is already running on %s; opening it." % url, flush=True)
            open_browser()
            return
        print("Port %d is in use by another program. Try: python3 %s --port %d"
              % (a.port, os.path.relpath(os.path.abspath(__file__)), a.port + 1), flush=True)
        raise SystemExit(1)
    print("ROG panel on %s%s" % (url, " (dry run)" if a.dry_run else ""), flush=True)
    open_browser()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
