"""セッションの実行と状態管理。

1ターン = エージェント CLI を1回サブプロセス実行。標準出力の各行をアダプタでパースし、
DB に保存しつつ Hub 経由で SSE 購読者へ配信する。プロセス終了 = ターン完了 = 入力待ち。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import adapters, worktree
from .db import DB

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Hub:
    """SSE 購読者へのブロードキャスト。購読者ごとに Queue を持つ。"""

    def __init__(self):
        self._subs: list = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, data: dict):
        msg = {"kind": kind, **data}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:  # 読まれていない購読者は切り捨て
                self.unsubscribe(q)


class SessionManager:
    def __init__(self, cfg: dict, db: DB | None = None, hub: Hub | None = None):
        self.cfg = cfg
        self.data_dir = Path(cfg["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or DB(self.data_dir / "agentdash.db")
        self.hub = hub or Hub()
        self.procs: dict = {}            # session_id -> Popen
        self.stopping: set = set()
        self._lock = threading.Lock()
        self._adapters: dict = {}
        self.db.mark_interrupted()

    # ---- アダプタ ----------------------------------------------------------------
    def adapter(self, name: str):
        if name not in self._adapters:
            settings = dict(self.cfg.get("adapters", {}).get(name) or {})
            if name == "local_llm":  # 会話履歴 (resume 用) はデータフォルダに置く
                settings.setdefault("conv_dir", str(self.data_dir / "conversations"))
            self._adapters[name] = adapters.create(name, settings)
        return self._adapters[name]

    def adapter_list(self) -> list:
        out = []
        for name, cls in adapters.REGISTRY.items():
            try:
                chk = self.adapter(name).check()
            except Exception as e:  # noqa: BLE001
                chk = {"ok": False, "detail": str(e)}
            out.append({"name": name, "label": cls.label, **chk})
        return out

    # ---- 操作 -------------------------------------------------------------------
    def create(self, name: str, repo_path: str, prompt: str, adapter: str | None = None,
               allowed_tools=None, permission_mode: str | None = None, use_worktree: bool | None = None,
               model: str | None = None) -> dict:
        d = self.cfg.get("defaults", {})
        adapter = adapter or self.cfg.get("default_adapter", "local_llm")
        self.adapter(adapter)  # 不明なら KeyError
        repo = Path(repo_path).expanduser()
        if not repo.is_dir():
            raise ValueError(f"フォルダが見つかりません: {repo}")
        if not prompt.strip():
            raise ValueError("最初の指示を入力してください")
        if isinstance(allowed_tools, str):
            allowed_tools = [t.strip() for t in allowed_tools.replace("\n", ",").split(",")]
        options = {
            "allowed_tools": [t for t in (allowed_tools if allowed_tools is not None else d.get("allowed_tools", [])) if t],
            "permission_mode": permission_mode or d.get("permission_mode") or "",
            "model": model or "",
        }
        use_wt = d.get("use_worktree", True) if use_worktree is None else use_worktree
        name = name.strip() or prompt.strip().splitlines()[0][:40]
        sid = self.db.create_session(name, str(repo.resolve()), adapter, options, status="running")
        wt = {"worktree_path": str(repo.resolve()), "branch": None, "base_commit": None}
        try:
            if use_wt and worktree.is_git_repo(repo):
                wt = worktree.create(repo, self.data_dir / "worktrees", sid, name)
            elif worktree.is_git_repo(repo):
                wt["base_commit"] = worktree.head_commit(repo)
                wt["branch"] = worktree.git(["branch", "--show-current"], repo, check=False).strip() or None
        except Exception:
            self.db.delete_session(sid)
            raise
        self.db.update_session(sid, **wt)
        self._start_turn(sid, prompt)
        return self.db.get_session(sid)

    def send(self, sid: int, prompt: str) -> dict:
        s = self._require(sid)
        if not prompt.strip():
            raise ValueError("指示を入力してください")
        if sid in self.procs:
            raise ValueError("実行中です。完了（入力待ち）になってから送ってください")
        if not Path(s["worktree_path"]).is_dir():
            raise ValueError(f"作業フォルダがありません: {s['worktree_path']}")
        self._start_turn(sid, prompt)
        return self.db.get_session(sid)

    def stop(self, sid: int) -> dict:
        self._require(sid)
        with self._lock:
            p = self.procs.get(sid)
            if p:
                self.stopping.add(sid)
        if p:
            _terminate(p)
        else:
            self._set_status(sid, "stopped")
        return self.db.get_session(sid)

    def complete(self, sid: int) -> dict:
        self._require(sid)
        if sid in self.procs:
            raise ValueError("実行中は完了にできません（先に停止してください）")
        self._set_status(sid, "done")
        return self.db.get_session(sid)

    def delete(self, sid: int, remove_worktree: bool = True) -> None:
        s = self._require(sid)
        if sid in self.procs:
            raise ValueError("実行中は削除できません（先に停止してください）")
        if remove_worktree and s.get("branch", "") and s["worktree_path"] != s["repo_path"] \
                and str(self.data_dir) in s["worktree_path"]:
            worktree.remove(s["repo_path"], s["worktree_path"], s["branch"], delete_branch=False)
        self.db.delete_session(sid)
        self.hub.publish("deleted", {"session_id": sid})

    def detail(self, sid: int) -> dict:
        s = self._require(sid)
        s["turns"] = self.db.turns_with_events(sid)
        return s

    def diff(self, sid: int, full=False) -> dict:
        s = self._require(sid)
        return worktree.diff(s["worktree_path"], s.get("base_commit"), full=full)

    def shutdown(self):
        for sid, p in list(self.procs.items()):
            self.stopping.add(sid)
            _terminate(p)

    # ---- 内部 -------------------------------------------------------------------
    def _require(self, sid: int) -> dict:
        s = self.db.get_session(sid)
        if not s:
            raise LookupError(f"セッション {sid} がありません")
        return s

    def _set_status(self, sid: int, status: str, **extra):
        self.db.update_session(sid, status=status, **extra)
        self.hub.publish("session", {"session": self.db.get_session(sid)})

    def _emit(self, turn_id: int, sid: int, type_: str, content: dict):
        ev = self.db.add_event(turn_id, type_, content)
        self.hub.publish("event", {"session_id": sid, "event": ev})

    def _start_turn(self, sid: int, prompt: str):
        s = self.db.get_session(sid)
        ad = self.adapter(s["adapter"])
        cmd = ad.build_command(prompt, Path(s["worktree_path"]), s.get("agent_session_id"), s["options"])
        turn_id = self.db.create_turn(sid, prompt)
        self.hub.publish("turn", {"session_id": sid, "turn": {"id": turn_id, "prompt": prompt,
                                                             "started_at": time.time(), "events": []}})
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # 内蔵ワーカー (python -m agentdash _agent ...) がどの作業フォルダからでも import できるように
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            p = subprocess.Popen(cmd, cwd=s["worktree_path"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, creationflags=NO_WINDOW,
                                 start_new_session=(os.name != "nt"))
        except OSError as e:
            self._emit(turn_id, sid, "stderr", {"text": f"起動できませんでした: {e}\nコマンド: {cmd[0]}"})
            self.db.end_turn(turn_id, -1, None)
            self._set_status(sid, "error")
            return
        with self._lock:
            self.procs[sid] = p
        self._set_status(sid, "running")
        threading.Thread(target=self._pump, args=(sid, turn_id, p, ad), daemon=True).start()

    def _pump(self, sid: int, turn_id: int, p: subprocess.Popen, ad):
        err_lines: list = []

        def read_err():
            for raw in p.stderr:
                t = raw.decode("utf-8", "replace").rstrip()
                if t:
                    err_lines.append(t)
                    self._emit(turn_id, sid, "stderr", {"text": t[:4000]})

        te = threading.Thread(target=read_err, daemon=True)
        te.start()
        cost, failed = None, False
        for raw in p.stdout:
            line = raw.decode("utf-8", "replace")
            try:
                ev = ad.parse_line(line)
            except Exception as e:  # noqa: BLE001  (パース失敗でターンを止めない)
                self._emit(turn_id, sid, "raw", {"text": f"パースエラー: {e}: {line[:500]}"})
                continue
            if ev is None:
                continue
            if ev.session_id:
                s = self.db.get_session(sid)
                if s and s.get("agent_session_id") != ev.session_id:
                    self.db.update_session(sid, agent_session_id=ev.session_id)
            if ev.cost is not None:
                cost = ev.cost
            if ev.type == "result" and ev.is_error:
                failed = True
            self._emit(turn_id, sid, ev.type, ev.content)
        code = p.wait()
        te.join(timeout=5)
        for f in (p.stdout, p.stderr):
            f.close()
        with self._lock:
            self.procs.pop(sid, None)
            stopped = sid in self.stopping
            self.stopping.discard(sid)
        self.db.end_turn(turn_id, code, cost)
        if stopped:
            self._emit(turn_id, sid, "system", {"subtype": "stopped", "text": "ユーザーが停止しました"})
            status = "stopped"
        elif failed or code != 0:
            status = "error"
        else:
            status = "waiting"
        self._set_status(sid, status)


def _terminate(p: subprocess.Popen):
    """子プロセスごと終了する (エージェントは子プロセスでコマンドを動かすため)。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                           creationflags=NO_WINDOW)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)
