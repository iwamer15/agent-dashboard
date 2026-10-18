"""HTTP サーバーとルーティング (標準ライブラリ http.server)。

codeflow への組み込み用に、API 部分は `handle_api(handler, manager)` として公開している。
codeflow 側の Handler から
    if self.path.startswith("/api/agents/") and agentdash.server.handle_api(self, manager): return
と呼べば同じ API が使える。静的ファイルは `STATIC_DIR` を codeflow の static 配下に置けばよい。
"""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .sessions import SessionManager, dumps

PREFIX = "/api/agents"


def _static_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = base / "agentdash" / "static"
    return p if p.is_dir() else Path(__file__).resolve().parent / "static"


STATIC_DIR = _static_dir()


# ---- 共通レスポンス ------------------------------------------------------------
def send_json(h: BaseHTTPRequestHandler, obj, status=200):
    body = dumps(obj).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def read_json(h: BaseHTTPRequestHandler) -> dict:
    n = int(h.headers.get("Content-Length") or 0)
    if not n:
        return {}
    data = json.loads(h.rfile.read(n).decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _sse(h: BaseHTTPRequestHandler, m: SessionManager):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "keep-alive")
    h.end_headers()
    q = m.hub.subscribe()
    try:
        h.wfile.write(b"retry: 2000\n\n: connected\n\n")
        h.wfile.flush()
        while True:
            try:
                msg = q.get(timeout=15)
                h.wfile.write(f"data: {dumps(msg)}\n\n".encode("utf-8"))
            except queue.Empty:
                h.wfile.write(b": ping\n\n")  # 接続維持 + 切断検知
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
        pass
    finally:
        m.hub.unsubscribe(q)


# ---- API ルーティング ------------------------------------------------------------
ROUTE = re.compile(rf"^{PREFIX}/sessions/(\d+)(?:/(messages|stop|diff|complete))?$")


def handle_api(h: BaseHTTPRequestHandler, m: SessionManager) -> bool:
    """/api/agents/... を処理したら True。"""
    u = urlparse(h.path)
    path, method = u.path.rstrip("/") or "/", h.command
    if not path.startswith(PREFIX):
        return False
    qs = parse_qs(u.query)
    try:
        if path == f"{PREFIX}/stream" and method == "GET":
            _sse(h, m)
        elif path == f"{PREFIX}/sessions" and method == "GET":
            send_json(h, {"sessions": m.db.list_sessions()})
        elif path == f"{PREFIX}/sessions" and method == "POST":
            b = read_json(h)
            s = m.create(b.get("name", ""), b.get("repo_path", ""), b.get("prompt", ""),
                         adapter=b.get("adapter") or None, allowed_tools=b.get("allowed_tools"),
                         permission_mode=b.get("permission_mode") or None,
                         use_worktree=b.get("use_worktree"), model=b.get("model") or None)
            send_json(h, {"session": s}, 201)
        elif path == f"{PREFIX}/meta" and method == "GET":
            send_json(h, {"adapters": m.adapter_list(), "defaults": m.cfg.get("defaults", {}),
                          "default_adapter": m.cfg.get("default_adapter"), "data_dir": str(m.data_dir),
                          "config_source": m.cfg.get("_source"),
                          "recent_repos": _recent_repos(m)})
        else:
            mt = ROUTE.match(path)
            if not mt:
                send_json(h, {"error": "not found"}, 404)
                return True
            sid, action = int(mt.group(1)), mt.group(2)
            if action is None and method == "GET":
                send_json(h, {"session": m.detail(sid)})
            elif action is None and method == "DELETE":
                m.delete(sid, remove_worktree=qs.get("keep_worktree", ["0"])[0] != "1")
                send_json(h, {"ok": True})
            elif action == "messages" and method == "POST":
                send_json(h, {"session": m.send(sid, read_json(h).get("prompt", ""))})
            elif action == "stop" and method == "POST":
                send_json(h, {"session": m.stop(sid)})
            elif action == "complete" and method == "POST":
                send_json(h, {"session": m.complete(sid)})
            elif action == "diff" and method == "GET":
                send_json(h, m.diff(sid, full=qs.get("full", ["0"])[0] == "1"))
            else:
                send_json(h, {"error": "method not allowed"}, 405)
    except LookupError as e:
        send_json(h, {"error": str(e)}, 404)
    except (ValueError, KeyError, RuntimeError) as e:
        send_json(h, {"error": str(e).strip("'\"")}, 400)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        send_json(h, {"error": f"{type(e).__name__}: {e}"}, 500)
    return True


def _recent_repos(m: SessionManager) -> list:
    seen = []
    for s in m.db.list_sessions():
        if s["repo_path"] not in seen:
            seen.append(s["repo_path"])
    return seen[:15]


# ---- スタンドアロン起動 ------------------------------------------------------------
def make_handler(m: SessionManager):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # アクセスログは出さない
            pass

        def _static(self):
            rel = urlparse(self.path).path.lstrip("/") or "index.html"
            p = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in p.parents or not p.is_file():
                self.send_error(404)
                return
            body = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not handle_api(self, m):
                self._static()

        def do_POST(self):
            if not handle_api(self, m):
                send_json(self, {"error": "not found"}, 404)

        def do_DELETE(self):
            self.do_POST()

    return Handler


def serve(cfg: dict, port: int | None = None, open_browser: bool = True):
    m = SessionManager(cfg)
    port = cfg.get("server", {}).get("port", 8790) if port is None else port
    httpd = None
    for p in ([0] if port == 0 else range(port, port + 20)):  # 使用中なら次のポートへ
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(m))
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"ポート {port}〜{port + 19} がすべて使用中です (--port で変更してください)")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Agent Dashboard: {url}")
    print(f"  データ: {m.data_dir}   設定: {cfg.get('_source') or '既定値'}")
    print("  終了するには Ctrl+C")
    if open_browser:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します…")
    finally:
        m.shutdown()
        httpd.server_close()
