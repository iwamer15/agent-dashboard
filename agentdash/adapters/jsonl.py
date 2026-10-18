"""内蔵ワーカー (_agent / _mock) が標準出力に書く JSON Lines を Event に変換する。

  {"type":"init", "session_id", "model", "cwd", "permission_mode"}
  {"type":"text", "text"}
  {"type":"tool_use", "id", "name", "input"}
  {"type":"tool_result", "id", "output", "is_error"}
  {"type":"system", "subtype", "text"}
  {"type":"result", "is_error", "text", "num_turns", "duration_ms", "usage", "session_id"}
"""
from __future__ import annotations

import json
from typing import Optional

from .base import Event, short


def parse_line(line: str) -> Optional[Event]:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return Event("raw", {"text": short(line)})
    if not isinstance(d, dict):
        return Event("raw", {"text": short(line)})
    t = d.get("type")
    sid = d.get("session_id")
    if t == "init":
        return Event("init", {"model": d.get("model"), "cwd": d.get("cwd"),
                              "permission_mode": d.get("permission_mode")}, session_id=sid)
    if t == "text":
        return Event("text", {"text": short(d.get("text", ""), 20000)})
    if t == "tool_use":
        return Event("tool_use", {"id": d.get("id"), "name": d.get("name"),
                                  "input": short(json.dumps(d.get("input"), ensure_ascii=False, indent=1))})
    if t == "tool_result":
        return Event("tool_result", {"id": d.get("id"), "output": short(d.get("output", "")),
                                     "is_error": bool(d.get("is_error"))})
    if t == "result":
        err = bool(d.get("is_error"))
        return Event("result", {"text": short(d.get("text", "")), "is_error": err,
                                "num_turns": d.get("num_turns"), "duration_ms": d.get("duration_ms"),
                                "usage": d.get("usage")}, session_id=sid, cost=d.get("cost"), is_error=err)
    return Event("system", {"subtype": d.get("subtype") or t, "text": short(d.get("text") or line, 600)})
