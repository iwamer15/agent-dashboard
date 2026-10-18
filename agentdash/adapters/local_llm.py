"""ローカルLLM アダプタ (内蔵エージェント agentdash.localagent を使う)。

OpenAI 互換 API を持つ LLM サーバーならどれでも接続できる:
  Ollama      http://127.0.0.1:11434/v1
  LM Studio   http://127.0.0.1:1234/v1
  llama.cpp   http://127.0.0.1:8080/v1   (llama-server)
  vLLM        http://<host>:8000/v1
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Optional

from . import jsonl
from .base import worker_command


class LocalLLMAdapter:
    name = "local_llm"
    label = "ローカルLLM（内蔵エージェント）"

    def __init__(self, base_url: str = "http://127.0.0.1:11434/v1", model: str = "qwen2.5-coder:7b",
                 api_key_env: str = "", tool_mode: str = "auto", max_steps: int = 30, timeout: int = 600,
                 cmd_timeout: int = 120, temperature: float = 0.2, use_proxy: bool = False,
                 conv_dir: str = "~/.agentdash/conversations"):
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env      # API キーは設定ファイルに書かず、環境変数名だけ指定する
        self.tool_mode = tool_mode
        self.max_steps = max_steps
        self.timeout = timeout
        self.cmd_timeout = cmd_timeout
        self.temperature = temperature
        self.use_proxy = use_proxy
        self.conv_dir = str(Path(conv_dir).expanduser())

    def build_command(self, prompt, workdir, resume_id, options):
        args = ["--base-url", self.base_url, "--model", options.get("model") or self.model,
                "--conv-dir", self.conv_dir, "--tool-mode", self.tool_mode,
                "--max-steps", str(self.max_steps), "--timeout", str(self.timeout),
                "--cmd-timeout", str(self.cmd_timeout), "--temperature", str(self.temperature),
                "--permission-mode", options.get("permission_mode") or "edit",
                "--allowed-tools", ",".join(options.get("allowed_tools") or [])]
        if self.api_key_env:
            args += ["--api-key-env", self.api_key_env]
        if self.use_proxy:
            args.append("--use-proxy")
        if resume_id:
            args += ["--resume", resume_id]
        return worker_command("_agent", args + ["--", prompt])

    def check(self):
        """LLM サーバーに /models を問い合わせる (モデル一覧も返す)。"""
        handlers = [] if self.use_proxy else [urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(self.base_url.rstrip("/") + "/models", timeout=3) as res:
                data = json.loads(res.read().decode("utf-8"))
            models = [m.get("id") for m in data.get("data", []) if m.get("id")]
            ok = not models or self.model in models
            detail = f"{self.base_url} に接続OK（モデル {len(models)} 件）"
            if not ok:
                detail += f"／設定のモデル {self.model} が見つかりません"
            return {"ok": True, "detail": detail, "models": models, "model": self.model}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{self.base_url} に接続できません: {e}", "models": [],
                    "model": self.model}

    def parse_line(self, line):
        return jsonl.parse_line(line)
