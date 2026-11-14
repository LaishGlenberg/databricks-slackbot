"""
Throwaway app: does an app get the same network access as a notebook?

Run it once, read the logs, delete it. Answers the only question a notebook
cannot: whether the Databricks Apps egress path can reach Slack.

Cost on Free Edition: $0. It also auto-stops after 24h, which is fine.
"""

import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def report() -> None:
    print("=== probe: app egress ===", flush=True)

    # Does app.yaml `resources:` + valueFrom actually inject the env var?
    wh = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    print(
        f"RESULT env DATABRICKS_WAREHOUSE_ID: {wh!r}"
        f" {'(BOUND)' if wh else '(NOT INJECTED)'}",
        flush=True,
    )

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection(("wss-primary.slack.com", 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname="wss-primary.slack.com") as tls:
                print(f"RESULT tls wss-primary.slack.com: OK ({tls.version()})", flush=True)
    except Exception as e:
        print(f"RESULT tls wss-primary.slack.com: BLOCKED {type(e).__name__}: {e}", flush=True)

    try:
        import requests
    except ImportError:
        # Must not look like an egress failure - that's a different cause.
        print(
            "RESULT https slack.com/api: SKIPPED (requests not installed)",
            flush=True,
        )
    else:
        try:
            r = requests.get("https://slack.com/api/api.test", timeout=15)
            print(f"RESULT https slack.com/api: {r.status_code} {r.text[:80]}", flush=True)
        except Exception as e:
            print(f"RESULT https slack.com/api: BLOCKED {type(e).__name__}: {e}", flush=True)

    print("=== probe: done ===", flush=True)


def serve_health() -> None:
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    report()  # once, at startup, so it lands in the app logs
    threading.Thread(target=serve_health, daemon=True).start()
    threading.Event().wait()  # keep the container alive
