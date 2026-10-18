# Agent Dashboard（v0.2）

ローカルLLMで動くコーディングエージェントのセッションを、ブラウザの1画面で起動・確認・管理するツール。

- **Python 3.9+ 標準ライブラリのみ**（`pip install` 不要）
- `127.0.0.1` だけで待ち受け、画面は外部通信なし（CDN・外部フォント・テレメトリなし）
- エージェントも内蔵（標準ライブラリのみ）。**OpenAI 互換 API** を持つ LLM サーバーにつなぐ
- セッションごとに **git worktree** を作るので、並列で動かしても互いのファイルを壊さない

## 準備: LLM サーバー

社内で用意された／自分で起動した LLM サーバーの URL とモデル名を `config.toml` に書きます。

| サーバー | `base_url` の例 |
|---|---|
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| llama.cpp（llama-server） | `http://127.0.0.1:8080/v1` |
| vLLM など社内サーバー | `http://<ホスト>:8000/v1` |

```toml
[adapters.local_llm]
base_url = "http://127.0.0.1:11434/v1"
model = "qwen2.5-coder:7b"
```

- モデルは **ツール呼び出し（function calling）に対応したコード向けモデル** を推奨（例: Qwen2.5-Coder 7B/14B 以上）。小さいモデルほど手順を間違えやすくなります
- ツール呼び出しに未対応のサーバー/モデルでも、自動で「本文にツールを書かせる方式」（`tool_mode = "text"`）に切り替えて動きます
- API キーが必要なサーバーは、キーを環境変数に入れて `api_key_env = "変数名"` を書きます（設定ファイルにキーは書かない）
- 既定ではプロキシを使いません（`use_proxy = true` で環境変数のプロキシを使用）
- 新規作成画面に接続状態（✓/✗）とサーバーのモデル一覧が出ます

## 起動

| 環境 | 方法 |
|---|---|
| Windows | `start.bat` をダブルクリック |
| Mac | `start.command` をダブルクリック |
| コマンド | `python -m agentdash [--port 8790] [--no-browser] [--config PATH]` |

ブラウザで `http://127.0.0.1:8790/` が開きます（使用中なら 8791… と自動でずらします）。
LLM サーバーが無くても、エージェントに「デモ（偽エージェント）」を選べば画面の動きを確認できます。

## 使い方

1. **＋ 新規セッション** … 名前・対象リポジトリ（絶対パス）・最初の指示・モデル・許可ツール・権限モードを入力 → worktree を作って実行開始
2. **一覧（左）** … 状態ごとに色分けしたカード。入力待ちが一番上
   - 実行中（青）／入力待ち（黄）／エラー（赤）／停止（灰）／完了（緑）
3. **詳細（右）** … ログのリアルタイム表示（ツール呼び出しと結果は折りたたみ）、追加指示（⌘/Ctrl + Enter）、**変更ファイル**タブ（開始時点からの `git diff --stat`・差分・新規ファイル・コミット）
4. **並列表示** … 完了以外のセッションのログを並べて表示。各枠から追加指示も送れる
5. **停止** … 実行中のプロセスを終了。**完了にする** … 済んだセッションを緑にする
6. **通知** … 「🔔 通知を有効化」を押すと、入力待ち／エラーでブラウザ通知

画面を使わずに1ターンだけ試す:

```bash
python -m agentdash run "README を1行で要約して" --cwd C:\work\repo [--adapter mock] [--worktree]
```

## 内蔵エージェント

LLM に「ツール」を渡し、LLM がツールを呼ぶ → 実行して結果を返す、を最終回答が出るまで繰り返します（1ターンの上限は `max_steps`）。

| ツール | 内容 | 必要な許可 |
|---|---|---|
| `list_files` / `read_file` / `search` | ファイル一覧・行番号付きで読む・正規表現検索 | 常に可 |
| `write_file` / `edit_file` | 新規作成・上書き／1箇所だけ置き換え | 権限モード「編集は自動許可」、または許可に `write` |
| `run_command` | 作業フォルダでコマンド実行（テストなど） | `run:コマンド` で前方一致したものだけ（例 `run:pytest`）。`run` で全部 |

- ファイル操作は**作業フォルダの外には出られません**
- `run:git status` のような個別許可では、`; & | $ < >` などでつないだコマンドは拒否します
- 権限モード: `edit`（編集は自動許可・コマンドは許可したものだけ。既定）／`readonly`（読むだけ・調査や計画を報告）／`allowlist`（許可に書いたものだけ）／`full`（すべて許可・注意）
- 追加指示は前回の会話の続きとして送られます（会話は `~/.agentdash/conversations/` に保存）

## 仕組み

**1ターン = 1プロセス**で実行し、標準出力の JSON Lines をアダプタがパース → SQLite に保存 → Server-Sent Events で画面へ配信します。
プロセス終了 = ターン完了 = 「入力待ち」（失敗や終了コード≠0 なら「エラー」）。サーバーを終了すると実行中のプロセスも止め、次回起動時に「停止」になります。

### データの保存先（`~/.agentdash/`）

| パス | 内容 |
|---|---|
| `agentdash.db` | sessions / turns / events（SQLite） |
| `worktrees/<リポジトリ名>-<ID>/` | セッションの作業フォルダ（ブランチ `agentdash/<ID>-<名前>`） |
| `conversations/<ID>.json` | 内蔵エージェントの会話履歴（追加指示で続きから） |

セッションを削除すると worktree フォルダは消えますが、**ブランチは残します**（成果を取り込むときは元リポジトリで `git merge agentdash/…` など）。

## 設定（`config.toml`）

読み込み順: `--config` → 環境変数 `AGENTDASH_CONFIG` → `~/.agentdash/config.toml` → 本体横の `config.toml`。
Python 3.9/3.10 は TOML を読めないので、同じ内容の `config.json` を読みます（`config.toml` を変えたら合わせて更新）。項目の説明は `config.toml` のコメントを参照。

## アダプタ

エージェント固有の処理は `agentdash/adapters/` に閉じ込め、本体はアダプタ経由でのみ操作します。

```python
class AgentAdapter(Protocol):
    name: str
    def build_command(self, prompt, workdir, resume_id, options) -> list[str]: ...
    def parse_line(self, line) -> Event | None: ...   # ログ・session_id・失敗を抽出
    def check(self) -> dict: ...                       # 利用可否（画面に表示）
```

| アダプタ | 内容 |
|---|---|
| `local_llm` | 内蔵エージェント（`agentdash/localagent.py`）＋ OpenAI 互換 API。既定 |
| `mock` | 偽エージェント（LLM なしで画面確認）。指示に「エラー」で失敗、「ゆっくり」で長めに動く |

別のエージェント（OpenCode、Aider などの CLI）を使う場合は、`adapters/` にクラスを1つ追加して `adapters/__init__.py` の `REGISTRY` に登録します。

## API（`/api/agents/...`）

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/agents/sessions` | 一覧 |
| POST | `/api/agents/sessions` | 作成＋初回実行 |
| GET | `/api/agents/sessions/{id}` | 詳細（ターン・イベント） |
| DELETE | `/api/agents/sessions/{id}` | 削除（worktree も削除。`?keep_worktree=1` で残す） |
| POST | `/api/agents/sessions/{id}/messages` | 追加指示 |
| POST | `/api/agents/sessions/{id}/stop` | 停止 |
| POST | `/api/agents/sessions/{id}/complete` | 完了にする |
| GET | `/api/agents/sessions/{id}/diff` | 変更内容（`?full=1` で差分本文も） |
| GET | `/api/agents/meta` | アダプタの接続状態・モデル一覧・既定値・最近のリポジトリ |
| GET | `/api/agents/stream` | 状態・ログの SSE |

## codeflow への組み込み

`agentdash/` を `codeflow/agents/` に移し、codeflow の Handler から呼ぶだけで同じ API が使えます。

```python
from .agents.server import handle_api
from .agents.sessions import SessionManager
manager = SessionManager(config.load())
# do_GET / do_POST / do_DELETE の先頭で
if self.path.startswith("/api/agents/") and handle_api(self, manager):
    return
```

## テスト

```bash
python -m unittest discover tests
```

偽 LLM サーバー（`tests/fake_llm.py`、OpenAI 互換の最小実装）と偽エージェントを使うので、LLM なしで動きます。
画面確認用に単体起動も可能: `python tests/fake_llm.py --port 11500`（`base_url = "http://127.0.0.1:11500/v1"`, `model = "fake-coder"`）。

## 未対応・今後

- 実際のローカルLLMでの精度確認・モデル選定
- Windows 実機での確認、PyInstaller での exe 化（内蔵ワーカーは `<exe> _agent ...` で起動する作りにしてある）
- スコープ外（MVP）: 認証・マルチユーザー、複数マシン管理、自動マージ・PR 作成
