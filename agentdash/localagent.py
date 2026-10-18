"""内蔵コーディングエージェント (標準ライブラリのみ)。

OpenAI 互換の Chat Completions API (Ollama / LM Studio / llama.cpp server / vLLM など) に接続し、
「LLM に聞く → ツールを実行 → 結果を返す」を最終回答が出るまで繰り返す。
ダッシュボードからは1ターン = 1プロセスとして起動され、進行を JSON Lines で標準出力に書く。

  python -m agentdash _agent --base-url http://127.0.0.1:11434/v1 --model qwen2.5-coder:7b \
      --conv-dir ~/.agentdash/conversations [--resume ID] -- "指示"

出力 (1行1 JSON):
  {"type":"init", "session_id", "model", "cwd", "permission_mode"}
  {"type":"text", "text"}
  {"type":"tool_use", "id", "name", "input"}
  {"type":"tool_result", "id", "output", "is_error"}
  {"type":"system", "subtype", "text"}
  {"type":"result", "is_error", "text", "num_turns", "duration_ms", "usage"}
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MAX_OUTPUT = 8000          # ツール結果を LLM に返すときの上限 (文字)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".idea", ".vscode"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def emit(d: dict):
    sys.stdout.write(json.dumps(d, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def clip(s: str, limit: int = MAX_OUTPUT) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n…（{len(s) - limit} 文字省略）"


# ---- ツール ------------------------------------------------------------------------
TOOL_SPECS = [
    ("list_files", "フォルダ内のファイル一覧を返す。pattern で絞り込み (例: *.py)。",
     {"path": ("string", "作業フォルダからの相対パス。省略時は作業フォルダ"),
      "pattern": ("string", "ファイル名のワイルドカード (省略可)")}, []),
    ("read_file", "テキストファイルを行番号付きで読む。大きいファイルは start_line / end_line で範囲指定。",
     {"path": ("string", "相対パス"), "start_line": ("integer", "開始行 (1始まり、省略可)"),
      "end_line": ("integer", "終了行 (省略可)")}, ["path"]),
    ("search", "正規表現でファイルの中身を検索し、該当行を返す。",
     {"pattern": ("string", "正規表現"), "path": ("string", "検索するフォルダ (省略可)"),
      "glob": ("string", "対象ファイルのワイルドカード (例: *.py、省略可)")}, ["pattern"]),
    ("write_file", "ファイルを新規作成または全体を上書きする。",
     {"path": ("string", "相対パス"), "content": ("string", "書き込む内容 (全文)")}, ["path", "content"]),
    ("edit_file", "ファイル内の old_text を new_text に置き換える。old_text はファイル内で1箇所だけ一致すること。",
     {"path": ("string", "相対パス"), "old_text": ("string", "置き換え前 (そのままの文字列)"),
      "new_text": ("string", "置き換え後")}, ["path", "old_text", "new_text"]),
    ("run_command", "作業フォルダでシェルコマンドを実行し、出力を返す (テスト実行など)。",
     {"command": ("string", "実行するコマンド")}, ["command"]),
]
READ_TOOLS = {"list_files", "read_file", "search"}
EDIT_TOOLS = {"write_file", "edit_file"}


def openai_tools() -> list:
    out = []
    for name, desc, props, req in TOOL_SPECS:
        out.append({"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "required": req,
                           "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()}}}})
    return out


MODES = ("edit", "readonly", "allowlist", "full")


class Permissions:
    """ダッシュボードの「許可」「権限モード」を内蔵ツールに当てはめる。

    許可の書き方 (画面と共通):
      write          ファイルの作成・編集
      run            すべてのコマンド
      run:git status そのコマンドで始まるものだけ (前方一致)
    読む系 (list_files / read_file / search) は常に許可。
    権限モード:
      edit      ファイル編集は常に許可、コマンドは許可したものだけ (既定)
      readonly  読むだけ (編集・コマンドは不可。計画や調査結果を報告させる)
      allowlist 許可に書いたものだけ
      full      すべて許可 (注意)
    """

    def __init__(self, allowed: list, mode: str):
        self.mode = mode if mode in MODES else "edit"
        self.allowed = [a.strip() for a in allowed if a.strip()]
        self.prefixes = []
        self.all_run = False
        self.write = False
        for a in self.allowed:
            key, _, rest = a.partition(":")
            key = key.strip().lower()
            if key == "write":
                self.write = True
            elif key == "run" and not rest.strip():
                self.all_run = True
            elif key == "run":
                self.prefixes.append(rest.strip())

    def check(self, tool: str, args: dict) -> str | None:
        """許可なら None、拒否なら理由。"""
        if self.mode == "full":
            return None
        if tool in READ_TOOLS:
            return None
        if self.mode == "readonly":
            return "読み取り専用モードのため、ファイルの変更とコマンド実行はできません。調べた結果や計画を文章で報告してください。"
        if tool in EDIT_TOOLS:
            if self.mode == "edit" or self.write:
                return None
            return "ファイルの編集は許可されていません（許可に write を追加してください）。"
        if tool == "run_command":
            cmd = str(args.get("command", "")).strip()
            if self.all_run or any(cmd == p or cmd.startswith(p + " ") for p in self.prefixes):
                if re.search(r"[;&|`$<>]", cmd) and not self.all_run:
                    return "許可は先頭のコマンドだけです。; & | ` $ < > を含むコマンドは実行できません。"
                return None
            allowed = ", ".join(self.prefixes) or "なし"
            return f"このコマンドは許可されていません（許可されているコマンド: {allowed}）。"
        return None


class Tools:
    def __init__(self, root: Path, perms: Permissions, cmd_timeout: int = 120):
        self.root = root.resolve()
        self.perms = perms
        self.cmd_timeout = cmd_timeout

    def _path(self, p: str | None) -> Path:
        target = (self.root / (p or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError("作業フォルダの外にはアクセスできません")
        return target

    def run(self, name: str, args: dict) -> tuple:
        """(出力, エラーか)"""
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return f"不明なツール: {name}（使えるのは {', '.join(n for n, *_ in TOOL_SPECS)}）", True
        denied = self.perms.check(name, args)
        if denied:
            return denied, True
        try:
            return clip(fn(**{k: v for k, v in args.items() if v is not None})), False
        except TypeError as e:
            return f"引数が正しくありません: {e}", True
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}", True

    def _walk(self, base: Path):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for f in sorted(files):
                if f == ".git":  # worktree の .git はファイル
                    continue
                yield Path(dirpath) / f

    def t_list_files(self, path=None, pattern=None):
        base = self._path(path)
        out = []
        for f in self._walk(base):
            rel = f.relative_to(self.root).as_posix()
            if pattern and not fnmatch.fnmatch(f.name, pattern):
                continue
            out.append(rel)
            if len(out) >= 500:
                out.append("…（500件で打ち切り。path か pattern で絞り込んでください）")
                break
        return "\n".join(out) or "（ファイルがありません）"

    def t_read_file(self, path, start_line=None, end_line=None):
        p = self._path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        s = max(1, int(start_line or 1))
        e = min(len(lines), int(end_line or (s + 399)))
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(s, e + 1))
        more = f"\n…（全 {len(lines)} 行。続きは start_line={e + 1} で読む）" if e < len(lines) else ""
        return (body or "（空のファイル）") + more

    def t_search(self, pattern, path=None, glob=None):
        rx = re.compile(pattern)
        out = []
        for f in self._walk(self._path(path)):
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                with open(f, encoding="utf-8", errors="strict") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            out.append(f"{f.relative_to(self.root).as_posix()}:{i}: {line.rstrip()[:300]}")
                            if len(out) >= 200:
                                return "\n".join(out) + "\n…（200件で打ち切り）"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(out) or "（一致なし）"

    def t_write_file(self, path, content):
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return f"{'上書き' if existed else '作成'}しました: {path}（{len(content.splitlines())} 行）"

    def t_edit_file(self, path, old_text, new_text):
        p = self._path(path)
        text = p.read_text(encoding="utf-8")
        n = text.count(old_text)
        if n == 0:
            raise ValueError("old_text が見つかりません。read_file で現在の内容を確認してください")
        if n > 1:
            raise ValueError(f"old_text が {n} 箇所にあります。前後の行を含めて1箇所に絞ってください")
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"編集しました: {path}"

    def t_run_command(self, command):
        try:
            r = subprocess.run(command, shell=True, cwd=str(self.root), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.cmd_timeout,
                               stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return f"{self.cmd_timeout} 秒でタイムアウトしました"
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return f"終了コード {r.returncode}\n{out.strip()}"


# ---- LLM 呼び出し --------------------------------------------------------------------
class LLMError(Exception):
    pass


class Client:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 600,
                 temperature: float = 0.2, use_proxy: bool = False):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        # 既定ではプロキシを使わない (社内プロキシ経由で外に出ないように / localhost 接続が壊れないように)
        handlers = [] if use_proxy else [urllib.request.ProxyHandler({})]
        self.opener = urllib.request.build_opener(*handlers)

    def chat(self, messages: list, tools: list | None) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": self.temperature, "stream": False}
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"LLM サーバーに接続できません（{self.url}）: {e.reason}") from None
        except TimeoutError:
            raise LLMError(f"LLM の応答が {self.timeout} 秒以内に返りませんでした") from None
        if not data.get("choices"):
            raise LLMError(f"想定外の応答: {json.dumps(data, ensure_ascii=False)[:500]}")
        return data


# ツール呼び出し機能 (function calling) が無いモデル向け: 本文にこの形式で書かせる
TEXT_TOOL_RE = re.compile(r"```tool\s*\n(.*?)\n```", re.S)


def text_tool_instructions() -> str:
    lines = ["ツールを使うときは、次の形式のブロックを1つだけ書いて返答を終えてください（結果は次のメッセージで渡します）:",
             "```tool", '{"name": "ツール名", "arguments": {"引数名": "値"}}', "```", "", "使えるツール:"]
    for name, desc, props, req in TOOL_SPECS:
        args = ", ".join(f"{k}{'' if k in req else '?'}: {t}" for k, (t, _) in props.items())
        lines.append(f"- {name}({args}) … {desc}")
    lines.append("作業が終わったら、ツールブロックを書かずに日本語で結果を報告してください。")
    return "\n".join(lines)


def parse_text_tools(content: str) -> list:
    calls = []
    for i, m in enumerate(TEXT_TOOL_RE.finditer(content or "")):
        try:
            d = json.loads(m.group(1))
            calls.append({"id": f"call_{uuid.uuid4().hex[:8]}", "name": d.get("name", ""),
                          "arguments": d.get("arguments") or {}})
        except ValueError:
            calls.append({"id": f"call_{uuid.uuid4().hex[:8]}", "name": "_invalid",
                          "arguments": {"raw": m.group(1)[:300]}})
    return calls


SYSTEM_PROMPT = """あなたはソフトウェア開発を手伝うコーディングエージェントです。
作業フォルダ: {cwd}
- 指示を達成するために、ツールでファイルを調べ、必要なら編集し、コマンドで確認します。
- 推測でファイルの中身を決めつけず、read_file / search で確認してから編集してください。
- パスは作業フォルダからの相対パスで指定します。
- 許可されていない操作はエラーになります。その場合は別の方法を考えるか、必要な許可をユーザーに伝えてください。
- 最後に、何をしたか・結果・残っている課題を日本語で簡潔に報告してください。
"""


# ---- エージェント本体 ------------------------------------------------------------------
def run(a) -> int:
    t0 = time.time()
    conv_dir = Path(a.conv_dir).expanduser()
    conv_dir.mkdir(parents=True, exist_ok=True)
    sid = a.resume or uuid.uuid4().hex
    conv_file = conv_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '', sid)}.json"
    root = Path.cwd()
    state = {"messages": [], "tool_mode": a.tool_mode}
    if a.resume and conv_file.exists():
        state = json.loads(conv_file.read_text(encoding="utf-8"))
    tool_mode = state.get("tool_mode") or a.tool_mode
    if not state["messages"]:
        sysmsg = SYSTEM_PROMPT.format(cwd=root)
        if tool_mode == "text":
            sysmsg += "\n" + text_tool_instructions()
        state["messages"].append({"role": "system", "content": sysmsg})
    state["messages"].append({"role": "user", "content": a.prompt})

    emit({"type": "init", "session_id": sid, "model": a.model, "cwd": str(root), "permission_mode": a.permission_mode})
    client = Client(a.base_url, a.model, os.environ.get(a.api_key_env, "") if a.api_key_env else "",
                    a.timeout, a.temperature, a.use_proxy)
    tools = Tools(root, Permissions(a.allowed_tools.split(",") if a.allowed_tools else [], a.permission_mode),
                  a.cmd_timeout)
    usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def save():
        state["tool_mode"] = tool_mode
        conv_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def finish(text, is_error, steps):
        save()
        emit({"type": "result", "is_error": is_error, "text": text, "num_turns": steps,
              "duration_ms": int((time.time() - t0) * 1000), "usage": usage, "session_id": sid})
        return 1 if is_error else 0

    final = ""
    for step in range(1, a.max_steps + 1):
        try:
            try:
                data = client.chat(state["messages"], openai_tools() if tool_mode != "text" else None)
            except LLMError as e:
                # ツール機能に対応していないサーバー/モデルなら、本文に書かせる方式に切り替えて再試行
                if tool_mode == "auto" and str(e).startswith("HTTP 4") and "tool" in str(e).lower():
                    tool_mode = "text"
                    state["messages"][0]["content"] += "\n" + text_tool_instructions()
                    emit({"type": "system", "subtype": "tool_mode",
                          "text": "このモデルはツール呼び出し機能に未対応のため、テキスト形式に切り替えました"})
                    data = client.chat(state["messages"], None)
                else:
                    raise
        except LLMError as e:
            return finish(str(e), True, step - 1)
        for k in usage:
            usage[k] += int((data.get("usage") or {}).get(k) or 0)
        msg = data["choices"][0].get("message") or {}
        content = msg.get("content") or ""
        native = msg.get("tool_calls") or []
        calls = []
        for tc in native:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else (args or {})
            except ValueError:
                args = {"_raw": args}
            calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", "name": fn.get("name", ""),
                          "arguments": args if isinstance(args, dict) else {"_raw": args}})
        text_calls = [] if calls else parse_text_tools(content)
        visible = TEXT_TOOL_RE.sub("", content).strip() if text_calls else content.strip()
        if visible:
            emit({"type": "text", "text": visible})
            final = visible

        if calls:  # function calling 方式
            state["messages"].append({"role": "assistant", "content": content or None, "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in calls]})
            for c in calls:
                out, err = execute(tools, c)
                state["messages"].append({"role": "tool", "tool_call_id": c["id"], "content": out})
        elif text_calls:  # テキスト方式
            state["messages"].append({"role": "assistant", "content": content})
            results = []
            for c in text_calls[:1]:  # 1回に1つ (順番に結果を見て判断させる)
                out, err = execute(tools, c)
                results.append(f"[{c['name']} の結果{'（エラー）' if err else ''}]\n{out}")
            state["messages"].append({"role": "user", "content": "\n\n".join(results)})
        else:  # ツールを使わない = 最終回答
            state["messages"].append({"role": "assistant", "content": content})
            return finish(final or "（応答が空でした）", not final, step)
        save()
    return finish(f"{a.max_steps} ステップで終わらなかったため中断しました。続ける場合は追加の指示を送ってください。",
                  True, a.max_steps)


def execute(tools: Tools, c: dict) -> tuple:
    emit({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
    if c["name"] == "_invalid":
        out, err = "ツールブロックの JSON を読めませんでした。形式を見直してください。", True
    else:
        out, err = tools.run(c["name"], c["arguments"])
    emit({"type": "tool_result", "id": c["id"], "output": out, "is_error": err})
    return out, err


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentdash _agent")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--conv-dir", required=True)
    ap.add_argument("--resume")
    ap.add_argument("--api-key-env", default="")
    ap.add_argument("--tool-mode", choices=["auto", "native", "text"], default="auto")
    ap.add_argument("--allowed-tools", default="")
    ap.add_argument("--permission-mode", default="edit")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--cmd-timeout", type=int, default=120)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--use-proxy", action="store_true")
    ap.add_argument("prompt")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
