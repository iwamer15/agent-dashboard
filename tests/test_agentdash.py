"""python -m unittest discover tests"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentdash import config, worktree  # noqa: E402
from agentdash.adapters import jsonl  # noqa: E402
from agentdash.adapters.local_llm import LocalLLMAdapter  # noqa: E402
from agentdash.db import DB  # noqa: E402
from agentdash.server import make_handler  # noqa: E402
from agentdash.sessions import SessionManager  # noqa: E402


def make_repo(d: Path) -> Path:
    repo = d / "repo"
    repo.mkdir()
    g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(g + ["commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def wait_status(m, sid, statuses, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        s = m.db.get_session(sid)
        if s["status"] in statuses:
            return s
        time.sleep(0.05)
    raise AssertionError(f"status が {statuses} になりませんでした: {m.db.get_session(sid)['status']}")


class TestAdapters(unittest.TestCase):
    def test_local_llm_build_command(self):
        a = LocalLLMAdapter(base_url="http://127.0.0.1:1/v1", model="m")
        cmd = a.build_command("hi", Path("."), None, {"allowed_tools": ["write", "run:git diff"],
                                                     "permission_mode": "edit", "model": "other"})
        self.assertEqual(cmd[-2:], ["--", "hi"])
        self.assertIn("_agent", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--allowed-tools") + 1], "write,run:git diff")
        self.assertEqual(cmd[cmd.index("--model") + 1], "other")  # セッションのモデル指定が優先
        cmd2 = a.build_command("more", Path("."), "abc", {})
        self.assertEqual(cmd2[cmd2.index("--resume") + 1], "abc")

    def test_parse_lines(self):
        p = jsonl.parse_line
        e = p(json.dumps({"type": "init", "session_id": "S1", "model": "m"}))
        self.assertEqual((e.type, e.session_id), ("init", "S1"))
        self.assertEqual(p(json.dumps({"type": "text", "text": "やります"})).content["text"], "やります")
        e = p(json.dumps({"type": "tool_use", "id": "t", "name": "run_command", "input": {"command": "ls"}}))
        self.assertEqual((e.type, e.content["name"]), ("tool_use", "run_command"))
        self.assertIn('"command": "ls"', e.content["input"])
        e = p(json.dumps({"type": "tool_result", "id": "t", "output": "out", "is_error": True}))
        self.assertEqual((e.content["output"], e.content["is_error"]), ("out", True))
        e = p(json.dumps({"type": "result", "is_error": True, "text": "NG", "session_id": "S1"}))
        self.assertEqual((e.type, e.is_error, e.session_id), ("result", True, "S1"))
        self.assertEqual(p("not json").type, "raw")
        self.assertIsNone(p("   "))


class TestDB(unittest.TestCase):
    def test_roundtrip(self):
        db = DB(":memory:")
        sid = db.create_session("n", "/r", "mock", {"allowed_tools": ["write"]})
        tid = db.create_turn(sid, "p")
        db.add_event(tid, "text", {"text": "こんにちは"})
        db.end_turn(tid, 0, 0.5)
        s = db.get_session(sid)
        self.assertEqual((s["turn_count"], s["total_cost"], s["last_message"]), (1, 0.5, "こんにちは"))
        self.assertEqual(s["options"]["allowed_tools"], ["write"])
        t = db.turns_with_events(sid)
        self.assertEqual(t[0]["events"][0]["content"]["text"], "こんにちは")
        db.mark_interrupted()  # running → stopped
        self.assertEqual(db.get_session(sid)["status"], "stopped")


class TestConfig(unittest.TestCase):
    def test_load_defaults_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"default_adapter": "mock", "defaults": {"permission_mode": "readonly"}}))
            cfg = config.load(str(p))
            self.assertEqual(cfg["default_adapter"], "mock")
            self.assertEqual(cfg["defaults"]["permission_mode"], "readonly")
            self.assertIn("allowed_tools", cfg["defaults"])  # 既定値とマージされる


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.repo = make_repo(d)
        cfg = config.load(str(d / "none.toml"))
        cfg["data_dir"] = str(d / "data")
        cfg["adapters"]["mock"] = {"delay": 0.02}
        self.m = SessionManager(cfg)

    def tearDown(self):
        self.m.shutdown()
        self.tmp.cleanup()

    def test_full_flow_with_worktree(self):
        s = self.m.create("demo", str(self.repo), "テスト追加して", adapter="mock")
        self.assertTrue(s["worktree_path"].startswith(str(self.m.data_dir)))
        self.assertTrue(s["branch"].startswith("agentdash/"))
        s = wait_status(self.m, s["id"], ["waiting", "error"])
        self.assertEqual(s["status"], "waiting")
        self.assertTrue(s["agent_session_id"])
        first_sid = s["agent_session_id"]
        d = self.m.diff(s["id"])
        self.assertIn("agentdash_demo.md", d["untracked"])
        self.assertFalse((self.repo / "agentdash_demo.md").exists())  # 元リポジトリは汚れない
        # 追加指示 → 同じ agent_session_id で resume
        self.m.send(s["id"], "続けて")
        s = wait_status(self.m, s["id"], ["waiting", "error"])
        self.assertEqual(s["agent_session_id"], first_sid)
        det = self.m.detail(s["id"])
        self.assertEqual(len(det["turns"]), 2)
        self.assertTrue(any(e["type"] == "result" for e in det["turns"][1]["events"]))
        self.m.complete(s["id"])
        self.assertEqual(self.m.db.get_session(s["id"])["status"], "done")
        wt = Path(s["worktree_path"])
        self.m.delete(s["id"])
        self.assertFalse(wt.exists())

    def test_error_and_stop(self):
        s = self.m.create("e", str(self.repo), "エラーにして", adapter="mock", use_worktree=False)
        self.assertEqual(s["worktree_path"], str(self.repo.resolve()))
        self.assertEqual(wait_status(self.m, s["id"], ["waiting", "error"])["status"], "error")
        s2 = self.m.create("slow", str(self.repo), "ゆっくり slow", adapter="mock", use_worktree=False)
        time.sleep(0.15)
        with self.assertRaises(ValueError):
            self.m.send(s2["id"], "割り込み")  # 実行中は送れない
        self.m.stop(s2["id"])
        self.assertEqual(wait_status(self.m, s2["id"], ["stopped", "waiting", "error"])["status"], "stopped")

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.m.create("x", "/no/such/dir", "p", adapter="mock")
        with self.assertRaises(KeyError):
            self.m.create("x", str(self.repo), "p", adapter="nope")


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.repo = make_repo(d)
        cfg = config.load(str(d / "none.toml"))
        cfg["data_dir"] = str(d / "data")
        cfg["adapters"]["mock"] = {"delay": 0.02}
        self.m = SessionManager(cfg)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.m))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.m.shutdown()
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=10) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_api_and_sse(self):
        # SSE を先に購読
        got = []
        stream = urllib.request.urlopen(self.base + "/api/agents/stream", timeout=10)

        def read_sse():
            try:
                for line in stream:
                    if line.startswith(b"data: "):
                        got.append(json.loads(line[6:]))
            except (OSError, ValueError):  # テスト終了時の切断
                pass

        threading.Thread(target=read_sse, daemon=True).start()
        time.sleep(0.1)
        code, r = self.req("POST", "/api/agents/sessions",
                           {"name": "api", "repo_path": str(self.repo), "prompt": "やって", "adapter": "mock"})
        self.assertEqual(code, 201, r)
        sid = r["session"]["id"]
        wait_status(self.m, sid, ["waiting"])
        time.sleep(0.2)
        kinds = {g["kind"] for g in got}
        self.assertTrue({"session", "turn", "event"} <= kinds, kinds)
        code, r = self.req("GET", "/api/agents/sessions")
        self.assertEqual([s["id"] for s in r["sessions"]], [sid])
        code, r = self.req("GET", f"/api/agents/sessions/{sid}")
        self.assertEqual(len(r["session"]["turns"]), 1)
        code, r = self.req("GET", f"/api/agents/sessions/{sid}/diff?full=1")
        self.assertIn("agentdash_demo.md", r["untracked"])
        code, r = self.req("POST", f"/api/agents/sessions/{sid}/messages", {"prompt": ""})
        self.assertEqual(code, 400)
        code, r = self.req("POST", f"/api/agents/sessions/{sid}/stop")
        self.assertEqual(r["session"]["status"], "stopped")
        code, r = self.req("GET", "/api/agents/sessions/999")
        self.assertEqual(code, 404)
        code, r = self.req("GET", "/api/agents/meta")
        self.assertIn("mock", [a["name"] for a in r["adapters"]])
        code, r = self.req("DELETE", f"/api/agents/sessions/{sid}")
        self.assertEqual(code, 200)
        stream.close()

    def test_static(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as res:
            self.assertIn(b"Agent Dashboard", res.read())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/../../etc/passwd", timeout=5)


if __name__ == "__main__":
    unittest.main()
