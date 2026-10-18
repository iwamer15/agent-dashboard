"""設定の読み込み。

探す順: --config で指定 → 環境変数 AGENTDASH_CONFIG → ~/.agentdash/config.toml → パッケージ横の config.toml
3.11+ は tomllib で TOML を読む。3.9/3.10 は同名の .json があればそれを読む (無ければ既定値)。
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

DEFAULTS = {
    "server": {"port": 8790},
    "data_dir": "~/.agentdash",
    "default_adapter": "local_llm",
    "defaults": {
        "permission_mode": "edit",
        "allowed_tools": ["run:git status", "run:git diff"],
        "use_worktree": True,
    },
    "adapters": {
        "local_llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:7b"},
        "mock": {"delay": 0.4},
    },
}


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller exe: exe の場所
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _read(path: Path) -> dict | None:
    if path.suffix == ".toml":
        try:
            import tomllib  # 3.11+
        except ImportError:
            j = path.with_suffix(".json")
            return _read(j) if j.exists() else None
        with open(path, "rb") as f:
            return tomllib.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(explicit: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    cands = [explicit, os.environ.get("AGENTDASH_CONFIG"),
             str(Path("~/.agentdash/config.toml").expanduser()), str(_app_dir() / "config.toml")]
    for c in cands:
        if not c:
            continue
        p = Path(c).expanduser()
        if p.exists() or p.with_suffix(".json").exists():
            data = _read(p) if p.exists() else _read(p.with_suffix(".json"))
            if data:
                _merge(cfg, data)
                cfg["_source"] = str(p)
            break
    cfg["data_dir"] = str(Path(cfg["data_dir"]).expanduser())
    return cfg
