"""デモ/テスト用アダプタ。内蔵エージェントと同じ JSON Lines を出す偽エージェントを動かす。

LLM が無くても画面・状態遷移・diff を確認できる。
  - 作業フォルダに agentdash_demo.md を追記する (diff 表示の確認用)
  - 指示に「エラー」/ "error" を含むと失敗ターン、「ゆっくり」/ "slow" を含むと長めに動く
"""
from __future__ import annotations

from . import jsonl
from .base import worker_command


class MockAdapter:
    name = "mock"
    label = "デモ（偽エージェント）"

    def __init__(self, delay: float = 0.4, **_):
        self.delay = delay

    def build_command(self, prompt, workdir, resume_id, options):
        args = ["--delay", str(self.delay)]
        if resume_id:
            args += ["--resume", resume_id]
        return worker_command("_mock", args + ["--", prompt])

    def parse_line(self, line):
        return jsonl.parse_line(line)

    def check(self):
        return {"ok": True, "detail": "常に利用可能（実際のAIは動きません）"}
