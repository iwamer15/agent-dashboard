"""テスト用の偽 LLM サーバー (OpenAI 互換 API の最小実装)。

決まった手順でツールを呼ぶだけ: list_files → read_file → edit_file → run_command → 最終回答。
単体でも起動できる (画面の動作確認用):
    python3 tests/fake_llm.py --port 11500 [--mode native|text|no_tools] [--delay 0.5]
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = [
    ("list_files", {}),
    ("read_file", {"path": "README.md"}),
    ("edit_file", {"path": "README.md", "old_text": "# demo", "new_text": "# demo\n\n編集しました。"}),
    ("run_command", {"command": "git status --short"}),
]


def step_of(messages):
    """最後の「本物の」ユーザー指示から、アシスタントが何回応答したか。"""
    last = max(i for i, m in enumerate(messages)
               if m["role"] == "user" and not str(m.get("content", "")).startswith("["))
    return sum(1 for m in messages[last + 1:] if m["role"] == "assistant")


class FakeLLM:
    def __init__(self, mode="native", delay=0.0):
        self.mode = mode          # native: tool_calls で返す / text: 本文に ```tool / no_tools: tools 付きは 400
        self.delay = delay
        self.requests = []

    def respond(self, body):
        self.requests.append(body)
        if self.mode == "no_tools" and body.get("tools"):
            return 400, {"error": {"message": "this model does not support tools"}}
        time.sleep(self.delay)
        msgs = body["messages"]
        n = step_of(msgs)
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        if n < len(SCRIPT):
            name, args = SCRIPT[n]
            if body.get("tools"):
                msg = {"role": "assistant", "content": f"{name} を使います。" if n == 0 else "",
                       "tool_calls": [{"id": f"c{n}", "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            else:
                block = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
                msg = {"role": "assistant", "content": f"手順{n + 1}です。\n```tool\n{block}\n```"}
        else:
            msg = {"role": "assistant", "content": "README.md を編集し、git status で変更を確認しました。"}
        return 200, {"id": "x", "object": "chat.completion", "model": body.get("model"),
                     "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}], "usage": usage}

    def handler(self):
        llm = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [{"id": "fake-coder"}]})
                else:
                    self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self._send(*llm.respond(body))

        return H

    def start(self, port=0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self.handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11500)
    ap.add_argument("--mode", default="native", choices=["native", "text", "no_tools"])
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()
    print("fake LLM:", FakeLLM(a.mode, a.delay).start(a.port))
    threading.Event().wait()
