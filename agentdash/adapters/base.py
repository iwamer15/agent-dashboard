"""エージェントアダプタの共通定義。

本体 (sessions.py) はアダプタ経由でのみエージェントを操作する。
社内版ではアダプタを差し替えるだけで動くようにするため、エージェント固有の
コマンド組み立て・出力パースはすべてここ (の実装クラス) に閉じ込める。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from typing import Protocol
except ImportError:  # pragma: no cover  (3.7 以前)
    Protocol = object  # type: ignore


@dataclass
class Event:
    """エージェント出力1行から取り出した情報。

    type:
      "init"        セッション開始 (session_id が入る)
      "text"        アシスタントの文章
      "thinking"    思考 (表示は折りたたみ)
      "tool_use"    ツール呼び出し  content={"name", "input", "id"}
      "tool_result" ツール結果      content={"output", "is_error", "id"}
      "result"      ターン終了      content={"text", "is_error", "duration_ms", "num_turns"}
      "system"      その他のシステム情報 (リトライ等)
      "stderr"      標準エラー出力 (本体が付ける)
      "raw"         JSON でない行
    """
    type: str
    content: dict = field(default_factory=dict)
    session_id: Optional[str] = None   # エージェント側のセッションID (resume 用)
    cost: Optional[float] = None       # このターンの費用 (USD)
    is_error: bool = False             # ターンが失敗扱いか (result で使う)


class AgentAdapter(Protocol):
    name: str
    label: str

    def build_command(self, prompt: str, workdir: Path, resume_id: Optional[str],
                      options: dict) -> list:
        """1ターン分の実行コマンド。options は allowed_tools / permission_mode / model など。"""
        ...

    def parse_line(self, line: str) -> Optional[Event]:
        """標準出力1行をパースする。表示不要の行は None。"""
        ...

    def check(self) -> dict:
        """利用可能か ({"ok": bool, "detail": str})。画面のアダプタ選択に表示する。"""
        ...


def worker_command(sub: str, args: list) -> list:
    """agentdash 内蔵のワーカー (_agent / _mock) を別プロセスで動かすコマンド。

    通常は `python -m agentdash <sub> ...`、PyInstaller の exe では `<exe> <sub> ...`。
    """
    import sys
    if getattr(sys, "frozen", False):
        return [sys.executable, sub, *args]
    return [sys.executable, "-m", "agentdash", sub, *args]


def short(v: Any, limit: int = 4000) -> str:
    """長い文字列を切り詰める (DB と画面が重くならないように)。"""
    s = v if isinstance(v, str) else str(v)
    if len(s) > limit:
        return s[:limit] + f"\n…（{len(s) - limit} 文字省略）"
    return s
