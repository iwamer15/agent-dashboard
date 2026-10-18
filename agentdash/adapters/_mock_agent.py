"""偽エージェント本体 (python -m agentdash _mock [--resume ID] -- 指示)。"""
import argparse
import json
import os
import sys
import time
import uuid


def emit(d):
    sys.stdout.write(json.dumps(d, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("prompt")
    a = ap.parse_args(argv)
    sid = a.resume or uuid.uuid4().hex
    p = a.prompt
    slow = "ゆっくり" in p or "slow" in p.lower()
    delay = a.delay * (6 if slow else 1)
    t0 = time.time()

    emit({"type": "init", "session_id": sid, "cwd": os.getcwd(), "model": "mock-1", "permission_mode": "edit"})
    time.sleep(delay)
    emit({"type": "text", "text": f"了解しました。「{p[:60]}」に取り組みます。"})
    time.sleep(delay)
    emit({"type": "tool_use", "id": "t1", "name": "list_files", "input": {}})
    time.sleep(delay)
    names = sorted(n for n in os.listdir(".") if n != ".git")[:20]
    emit({"type": "tool_result", "id": "t1", "output": "\n".join(names), "is_error": False})
    time.sleep(delay)
    if "エラー" in p or "error" in p.lower():
        emit({"type": "result", "is_error": True, "session_id": sid, "num_turns": 1,
              "text": "デモ: 指示に「エラー」が含まれていたため失敗扱いにしました"})
        sys.exit(1)
    emit({"type": "tool_use", "id": "t2", "name": "write_file",
          "input": {"path": "agentdash_demo.md", "content": p[:80]}})
    with open("agentdash_demo.md", "a", encoding="utf-8") as f:
        f.write(f"- {time.strftime('%H:%M:%S')} {p}\n")
    time.sleep(delay)
    emit({"type": "tool_result", "id": "t2", "output": "agentdash_demo.md を更新しました", "is_error": False})
    time.sleep(delay)
    emit({"type": "text", "text": "完了しました。`agentdash_demo.md` に指示内容を追記しています。次の指示をどうぞ。"})
    emit({"type": "result", "is_error": False, "session_id": sid, "num_turns": 3,
          "duration_ms": int((time.time() - t0) * 1000), "text": "完了しました。"})


if __name__ == "__main__":
    main()
