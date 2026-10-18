"""python -m agentdash [--port 8790] [--no-browser] [--config PATH]

  run サブコマンド: 画面を使わず1ターンだけ実行してイベントを表示する (アダプタの動作確認用)
    python -m agentdash run "READMEを要約して" --cwd ~/repo [--adapter mock]
"""
import argparse
import multiprocessing
import sys

from . import config


def _safe_console():
    """日本語を表示できないコンソール (cp932 など) で print が落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _run_once(cfg, a):
    import time
    from .sessions import SessionManager

    if a.data_dir:
        cfg["data_dir"] = a.data_dir
    m = SessionManager(cfg)
    q = m.hub.subscribe()
    s = m.create(a.name or "cli", a.cwd, a.prompt, adapter=a.adapter, use_worktree=a.worktree)
    print(f"session #{s['id']}  workdir={s['worktree_path']}")
    while True:
        msg = q.get()
        if msg["kind"] == "event":
            e = msg["event"]
            print(f"[{e['type']}] {str(e['content'])[:300]}")
        elif msg["kind"] == "session" and msg["session"]["status"] != "running":
            print(f"--> status={msg['session']['status']}  agent_session_id={msg['session']['agent_session_id']}")
            break
    time.sleep(0.1)


def main():
    # 内蔵ワーカー (アダプタが別プロセスとして起動する)。exe でも同じ入口を使う
    if len(sys.argv) > 1 and sys.argv[1] in ("_agent", "_mock"):
        sub, rest = sys.argv[1], sys.argv[2:]
        if sub == "_agent":
            from .localagent import main as worker
        else:
            from .adapters._mock_agent import main as worker
        worker(rest)
        return
    _safe_console()
    ap = argparse.ArgumentParser(prog="agentdash", description="コーディングエージェントのセッション管理ダッシュボード")
    ap.add_argument("--port", type=int, default=None, help="待ち受けポート (使用中なら次を自動選択、0 で空きを自動)")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--config", default=None, help="config.toml のパス")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="1ターンだけ実行してイベントを表示")
    r.add_argument("prompt")
    r.add_argument("--cwd", default=".")
    r.add_argument("--name", default="")
    r.add_argument("--adapter", default=None)
    r.add_argument("--worktree", action="store_true", help="git worktree を作って実行")
    r.add_argument("--data-dir", default=None)
    a = ap.parse_args()
    cfg = config.load(a.config)
    if a.cmd == "run":
        _run_once(cfg, a)
        return
    from .server import serve
    serve(cfg, a.port, not a.no_browser)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
