#!/usr/bin/env python3
"""Tiny HTTP listener used only by scripts/ha_drill.sh to prove the webhook retry
worker's `SKIP LOCKED` fix (WP-24) actually prevents double delivery across replicas.

Fails the first `--fail-count` requests with 500 (so the delivery genuinely becomes
"due" for the periodic retry worker to pick up), then succeeds with 200 forever after.
Every request is appended to `--log` as one line (timestamp + running hit count) — the
drill counts lines in that file rather than trusting anything in-process, since the
whole point is proving no request was silently swallowed or duplicated.

Usage: python webhook_probe.py --port 8099 --fail-count 2 --log /path/to/hits.log
"""
import argparse
import datetime
import http.server
import threading


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fail-count", type=int, default=0)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()

    lock = threading.Lock()
    state = {"hits": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            with lock:
                state["hits"] += 1
                hit_no = state["hits"]
                should_fail = hit_no <= args.fail_count
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(
                    f"{datetime.datetime.utcnow().isoformat()}Z hit={hit_no} "
                    f"fail={should_fail} bytes={len(body)}\n"
                )
            self.send_response(500 if should_fail else 200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, fmt, *a):  # silence stdlib's default stderr access log
            pass

    server = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
