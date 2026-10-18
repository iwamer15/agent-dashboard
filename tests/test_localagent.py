"""内蔵エージェント (ローカルLLM アダプタ) のテスト。偽 LLM サーバーを使う。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentdash import config  # noqa: E402
from agentdash.localagent import Permissions, Tools, parse_text_tools  # noqa: E402
from agentdash.sessions import SessionManager  # noqa: E402
from fake_llm import FakeLLM  # noqa: E402
from test_agentdash import make_repo, wait_status  # noqa: E402


class TestPermissions(unittest.TestCase):
    def test_modes_and_prefixes(self):
        p = Permissions(["run:git status", "run:pytest"], "edit")
        self.assertIsNone(p.check("read_file", {}))
        self.assertIsNone(p.check("edit_file", {}))  # edit モードは編集可
        self.assertIsNone(p.check("run_command", {"command": "git status --short"}))
        self.assertIsNone(p.check("run_command", {"command": "pytest"}))
        self.assertIsNotNone(p.check("run_command", {"command": "rm -rf ."}))
        self.assertIsNotNone(p.check("run_command", {"command": "git status; rm -rf ."}))
        self.assertIsNotNone(p.check("run_command", {"command": "git statusx"}))
        d = Permissions([], "allowlist")
        self.assertIsNotNone(d.check("write_file", {}))
        self.assertIsNone(Permissions(["write"], "allowlist").check("write_file", {}))
        ro = Permissions(["run"], "readonly")
        self.assertIsNotNone(ro.check("edit_file", {}))
        self.assertIsNotNone(ro.check("run_command", {"command": "ls"}))
        self.assertIsNone(Permissions([], "full").check("run_command", {"command": "ls"}))
        self.assertIsNone(Permissions(["run"], "edit").check("run_command", {"command": "ls | wc"}))
        self.assertEqual(Permissions([], "unknown").mode, "edit")


class TestTools(unittest.TestCase):
    def test_tools_and_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            t = Tools(root, Permissions(["run"], "edit"))
            self.assertIn("a.py", t.run("list_files", {})[0])
            self.assertIn("    2  y = 2", t.run("read_file", {"path": "a.py"})[0])
            self.assertIn("a.py:1:", t.run("search", {"pattern": r"x\s*="})[0])
            out, err = t.run("edit_file", {"path": "a.py", "old_text": "y = 2", "new_text": "y = 3"})
            self.assertFalse(err, out)
            self.assertIn("y = 3", (root / "a.py").read_text())
            out, err = t.run("edit_file", {"path": "a.py", "old_text": "nothing", "new_text": ""})
            self.assertTrue(err)
            out, err = t.run("read_file", {"path": "../outside.txt"})
            self.assertTrue(err)
            self.assertIn("外", out)
            out, err = t.run("write_file", {"path": "sub/b.txt", "content": "hi"})
            self.assertFalse(err)
            self.assertIn("終了コード 0", t.run("run_command", {"command": "echo ok"})[0])
            self.assertTrue(t.run("no_such_tool", {})[1])

    def test_parse_text_tools(self):
        c = parse_text_tools('説明\n```tool\n{"name": "read_file", "arguments": {"path": "a"}}\n```')
        self.assertEqual((c[0]["name"], c[0]["arguments"]), ("read_file", {"path": "a"}))
        self.assertEqual(parse_text_tools("```tool\n{broken\n```")[0]["name"], "_invalid")


class TestLocalLLMSessions(unittest.TestCase):
    def make(self, mode, **adapter):
        self.llm = FakeLLM(mode)
        url = self.llm.start()
        cfg = config.load(str(self.d / "none.toml"))
        cfg["data_dir"] = str(self.d / "data")
        cfg["adapters"]["local_llm"] = {"base_url": url, "model": "fake-coder", **adapter}
        self.m = SessionManager(cfg)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.repo = make_repo(self.d)
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "readme"],
                       cwd=self.repo, check=True)
        self.m = None
        self.llm = None

    def tearDown(self):
        if self.m:
            self.m.shutdown()
        if self.llm:
            self.llm.stop()
        self.tmp.cleanup()

    def run_flow(self):
        s = self.m.create("llm", str(self.repo), "README を直して", adapter="local_llm",
                          allowed_tools=["run:git status"])
        s = wait_status(self.m, s["id"], ["waiting", "error"], timeout=30)
        det = self.m.detail(s["id"])
        types = [e["type"] for e in det["turns"][0]["events"]]
        self.assertEqual(s["status"], "waiting", det["turns"][0]["events"])
        self.assertEqual(types.count("tool_use"), 4)
        results = [e["content"] for e in det["turns"][0]["events"] if e["type"] == "tool_result"]
        self.assertFalse(any(r["is_error"] for r in results), results)
        self.assertIn("README.md", results[3]["output"])  # git status に変更が出る
        self.assertIn("編集しました", (Path(s["worktree_path"]) / "README.md").read_text(encoding="utf-8"))
        return s

    def test_native_tool_calls_and_resume(self):
        self.make("native")
        s = self.run_flow()
        first = len(self.llm.requests[-1]["messages"])
        self.assertEqual(self.llm.requests[0]["model"], "fake-coder")
        self.m.send(s["id"], "もう一度")
        s = wait_status(self.m, s["id"], ["waiting", "error"], timeout=30)
        # resume: 前回の会話の続きとして送られている
        self.assertGreater(len(self.llm.requests[-1]["messages"]), first)
        self.assertEqual(self.llm.requests[-1]["messages"][0]["role"], "system")

    def test_text_mode(self):
        self.make("text", tool_mode="text")
        self.run_flow()
        self.assertNotIn("tools", self.llm.requests[0])

    def test_auto_fallback_when_tools_unsupported(self):
        self.make("no_tools")
        s = self.run_flow()
        evs = self.m.detail(s["id"])["turns"][0]["events"]
        self.assertTrue(any(e["type"] == "system" and "テキスト形式" in e["content"]["text"] for e in evs))

    def test_readonly_mode_denies_edit(self):
        self.make("native")
        s = self.m.create("ro", str(self.repo), "直して", adapter="local_llm", permission_mode="readonly")
        s = wait_status(self.m, s["id"], ["waiting", "error"], timeout=30)
        res = [e["content"] for e in self.m.detail(s["id"])["turns"][0]["events"] if e["type"] == "tool_result"]
        self.assertTrue(res[2]["is_error"])  # edit_file が拒否される
        self.assertEqual((Path(s["worktree_path"]) / "README.md").read_text(encoding="utf-8"), "# demo\n")

    def test_connection_error(self):
        cfg = config.load(str(self.d / "none.toml"))
        cfg["data_dir"] = str(self.d / "data")
        cfg["adapters"]["local_llm"] = {"base_url": "http://127.0.0.1:9/v1", "model": "x", "timeout": 5}
        self.m = SessionManager(cfg)
        self.assertFalse(self.m.adapter("local_llm").check()["ok"])
        s = self.m.create("ng", str(self.repo), "hi", adapter="local_llm", use_worktree=False)
        s = wait_status(self.m, s["id"], ["waiting", "error"], timeout=30)
        self.assertEqual(s["status"], "error")
        last = self.m.detail(s["id"])["turns"][0]["events"][-1]
        self.assertIn("接続できません", last["content"]["text"])

    def test_check_lists_models(self):
        self.make("native")
        chk = self.m.adapter("local_llm").check()
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["models"], ["fake-coder"])


if __name__ == "__main__":
    unittest.main()
