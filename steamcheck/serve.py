"""本地只读视图：同一份快照的网页呈现。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from .engine import analyse

WEB = Path(__file__).resolve().parent / "web"


def build(root: Path, as_of: datetime | None) -> dict:
    return analyse(root, as_of).as_dict()


def handler_factory(root: Path):
    index = (WEB / "index.html").read_bytes()
    app = (WEB / "app.js").read_bytes()
    render = (WEB / "render.js").read_bytes()
    css = (WEB / "app.css").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 保持安静，便于测试
            pass

        def _send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in {"/", "/index.html"}:
                self._send(index, "text/html; charset=utf-8")
            elif path == "/app.js":
                self._send(app, "text/javascript; charset=utf-8")
            elif path == "/render.js":
                self._send(render, "text/javascript; charset=utf-8")
            elif path == "/app.css":
                self._send(css, "text/css; charset=utf-8")
            elif path == "/api/snapshot":
                as_of = None
                query = parse_qs(urlsplit(self.path).query)
                if query.get("as_of"):
                    try:
                        as_of = datetime.fromisoformat(query["as_of"][0])
                    except ValueError:
                        self.send_error(400, "as_of must be an ISO timestamp with timezone")
                        return
                try:
                    payload = build(root, as_of)
                except ValueError as error:
                    self.send_error(400, "invalid as_of or no data yet")
                    return
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            else:
                self.send_error(404)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="steamcheck-serve")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_factory(args.root))
    print(f"http://127.0.0.1:{args.port}/  （Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
