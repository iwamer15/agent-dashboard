// Agent Dashboard フロントエンド (ビルド不要・外部ライブラリなし)
"use strict";

const API = "api/agents";
const STATUS = {
  running: { label: "実行中", order: 1 },
  waiting: { label: "入力待ち", order: 0 },
  error: { label: "エラー", order: 2 },
  stopped: { label: "停止", order: 3 },
  done: { label: "完了", order: 4 },
};
const TOOL_PRESETS = ["write", "run:git status", "run:git diff", "run:git log", "run:python", "run:pytest",
  "run:npm test", "run"];

const state = {
  sessions: new Map(),   // id -> session
  details: new Map(),    // id -> {turns:[...]}
  selected: null,
  view: "detail",
  tab: "log",
  meta: null,
};
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- API ---------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}

// ---- 表示ヘルパ ----------------------------------------------------------------
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "たった今";
  if (s < 3600) return `${Math.floor(s / 60)}分前`;
  if (s < 86400) return `${Math.floor(s / 3600)}時間前`;
  return new Date(ts * 1000).toLocaleDateString();
}
const base = (p) => String(p || "").split(/[\\/]/).filter(Boolean).pop() || p;
const cost = (c) => (c ? `$${Number(c).toFixed(c < 0.1 ? 4 : 2)}` : "");
const badge = (st) => `<span class="badge ${st} s-${st}">${STATUS[st]?.label || st}</span>`;

// 最小限の Markdown: ``` コードブロックと `インライン`
function md(text) {
  const out = [];
  String(text).split(/```/).forEach((chunk, i) => {
    if (i % 2) out.push(`<pre><code>${esc(chunk.replace(/^[\w+-]*\n/, ""))}</code></pre>`);
    else out.push(esc(chunk).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>"));
  });
  return out.join("");
}

function toolSummary(name, input) {
  try {
    const o = JSON.parse(input);
    return o.command || o.file_path || o.pattern || o.path || o.url || o.query || o.description || "";
  } catch { return ""; }
}

function renderEvent(e) {
  const c = e.content || {};
  switch (e.type) {
    case "text": return `<div class="ev text">${md(c.text)}</div>`;
    case "thinking": return `<div class="ev thinking">${esc(c.text)}</div>`;
    case "multi": return (c.parts || []).map((p) => renderEvent({ type: p.type, content: p })).join("");
    case "tool_use":
      return `<details class="ev tool"><summary><span class="tname">${esc(c.name)}</span><span class="tsum">${esc(toolSummary(c.name, c.input))}</span></summary><pre>${esc(c.input)}</pre></details>`;
    case "tool_result": {
      const first = (c.output || "").split("\n")[0].slice(0, 120);
      return `<details class="ev tool tool-result ${c.is_error ? "err" : ""}"><summary><span class="tname">${c.is_error ? "✗ 結果" : "↳ 結果"}</span><span class="tsum">${esc(first)}</span></summary><pre>${esc(c.output)}</pre></details>`;
    }
    case "result":
      if (c.is_error) {
        const hint = /接続できません/.test(c.text || "") ? "\n→ LLM サーバー（Ollama など）が起動しているか、config.toml の base_url を確認してください" : "";
        return `<div class="ev result err">✗ ターン失敗${c.subtype && c.subtype !== "success" ? `（${esc(c.subtype)}）` : ""}: ${esc(c.text)}${esc(hint)}</div>`;
      }
      return `<div class="ev result"><span>✓ ターン完了</span>${c.num_turns ? `<span>${c.num_turns} ステップ</span>` : ""}${c.duration_ms ? `<span>${(c.duration_ms / 1000).toFixed(1)} 秒</span>` : ""}${c.usage?.prompt_tokens ? `<span>入力 ${c.usage.prompt_tokens.toLocaleString()} / 出力 ${c.usage.completion_tokens.toLocaleString()} トークン</span>` : ""}</div>`;
    case "init": return `<div class="ev sys">開始: model=${esc(c.model || "?")} / permission=${esc(c.permission_mode || "?")}</div>`;
    case "stderr": return `<div class="ev stderr">${esc(c.text)}</div>`;
    case "system": return `<div class="ev sys">[${esc(c.subtype || "system")}] ${esc(c.text)}</div>`;
    default: return `<div class="ev sys">${esc(c.text || JSON.stringify(c))}</div>`;
  }
}

function renderTurn(t, idx) {
  const head = `<div class="turn-sep">ターン ${idx + 1} · ${new Date(t.started_at * 1000).toLocaleTimeString()}${t.cost ? " · " + cost(t.cost) : ""}</div>`;
  return head + `<div class="ev user">${esc(t.prompt)}</div>` + (t.events || []).map(renderEvent).join("");
}

// ---- 一覧 ---------------------------------------------------------------------
function sortedSessions() {
  const q = $("#search").value.trim().toLowerCase();
  const hide = $("#hideDone").checked;
  return [...state.sessions.values()]
    .filter((s) => !q || (s.name + " " + s.repo_path + " " + (s.branch || "")).toLowerCase().includes(q))
    .filter((s) => !hide || !["done", "stopped"].includes(s.status))
    .sort((a, b) => (STATUS[a.status]?.order ?? 9) - (STATUS[b.status]?.order ?? 9) || b.updated_at - a.updated_at);
}

function renderList() {
  const list = $("#list");
  const items = sortedSessions();
  let html = "", lastGroup = null;
  for (const s of items) {
    if (s.status !== lastGroup) { html += `<div class="group-label">${STATUS[s.status]?.label || s.status}</div>`; lastGroup = s.status; }
    html += `<div class="card s-${s.status} ${s.id === state.selected ? "sel" : ""}" data-id="${s.id}">
      <div class="top"><span class="name">${esc(s.name)}</span>${badge(s.status)}</div>
      <div class="sub">${esc(base(s.repo_path))}${s.branch ? " · " + esc(s.branch) : ""} · ${esc(s.adapter)}</div>
      ${s.last_message ? `<div class="msg">${esc(s.last_message)}</div>` : ""}
      <div class="foot"><span>${s.turn_count} ターン</span>${s.total_cost ? `<span>${cost(s.total_cost)}</span>` : ""}<span>${ago(s.updated_at)}</span></div>
    </div>`;
  }
  list.innerHTML = html || `<div class="grid-empty">${state.sessions.size ? "条件に合うセッションはありません" : "まだセッションはありません"}</div>`;
  // ヘッダーの件数
  const cnt = {};
  for (const s of state.sessions.values()) cnt[s.status] = (cnt[s.status] || 0) + 1;
  $("#counts").innerHTML = Object.keys(STATUS).filter((k) => cnt[k])
    .map((k) => `<span class="count-chip s-${k}"><i style="background:var(--c)"></i>${STATUS[k].label} <b>${cnt[k]}</b></span>`).join("");
  const waiting = cnt.waiting || 0;
  document.title = (waiting ? `(${waiting}) ` : "") + "Agent Dashboard";
  $("#empty").classList.toggle("hidden", state.selected != null);
}

// ---- 詳細 ---------------------------------------------------------------------
async function select(id) {
  state.selected = id;
  renderList();
  if (state.view !== "detail") setView("detail");
  if (!state.details.has(id)) await loadDetail(id);
  renderDetail(true);
  if (state.tab === "diff") loadDiff();
  else refreshDiffCount();
}

async function loadDetail(id) {
  const { session } = await api(`/sessions/${id}`);
  state.details.set(id, { turns: session.turns });
  delete session.turns;
  state.sessions.set(id, session);
}

function renderDetail(scrollBottom) {
  const id = state.selected;
  const s = state.sessions.get(id);
  $("#detail").classList.toggle("hidden", !s);
  $("#empty").classList.toggle("hidden", !!s);
  if (!s) return;
  $("#dName").textContent = s.name;
  const b = $("#dStatus");
  b.className = `badge ${s.status} s-${s.status}`;
  b.textContent = STATUS[s.status]?.label || s.status;
  const o = s.options || {};
  $("#dMeta").innerHTML = [
    `作業フォルダ <code title="クリックでコピー" data-copy="${esc(s.worktree_path)}">${esc(s.worktree_path)}</code>`,
    s.branch ? `ブランチ <code data-copy="${esc(s.branch)}">${esc(s.branch)}</code>` : "",
    `元リポジトリ ${esc(s.repo_path)}`,
    `エージェント ${esc(s.adapter)}${o.model ? " / " + esc(o.model) : ""}${o.permission_mode ? " / " + esc(o.permission_mode) : ""}`,
    o.allowed_tools?.length ? `許可 ${esc(o.allowed_tools.join(", "))}` : "",
    s.total_cost ? `費用 ${cost(s.total_cost)}` : "",
  ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");
  const running = s.status === "running";
  $("#stopBtn").disabled = !running;
  $("#doneBtn").disabled = running || s.status === "done";
  $("#delBtn").disabled = running;
  $("#sendBtn").disabled = running;
  $("#prompt").placeholder = running ? "実行中… 完了（入力待ち）になると送信できます" : "追加の指示（⌘/Ctrl + Enter で送信）";

  const pane = $("#logPane");
  const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  const d = state.details.get(id);
  pane.innerHTML = (d?.turns || []).map(renderTurn).join("") + (running ? `<div class="working">エージェントが作業中です…</div>` : "");
  if (scrollBottom || atBottom) pane.scrollTop = pane.scrollHeight;
}

async function refreshDiffCount() {
  const id = state.selected;
  try {
    const d = await api(`/sessions/${id}/diff`);
    if (id !== state.selected) return;
    const n = (d.files?.length || 0) + (d.untracked?.length || 0);
    $("#diffCount").textContent = n ? n : "";
  } catch { /* 表示だけなので無視 */ }
}

async function loadDiff() {
  const id = state.selected;
  const pane = $("#diffPane");
  pane.innerHTML = `<p class="hint">読み込み中…</p>`;
  try {
    const d = await api(`/sessions/${id}/diff?full=1`);
    if (id !== state.selected) return;
    if (!d.git) { pane.innerHTML = `<p class="hint">git リポジトリではないため変更内容を表示できません。</p>`; return; }
    const n = d.files.length + d.untracked.length;
    $("#diffCount").textContent = n ? n : "";
    let h = "";
    if (!n && !d.commits?.length) h += `<p class="hint">セッション開始時点からの変更はありません。</p>`;
    if (d.commits?.length) h += `<h3>コミット（${d.commits.length}）</h3><table>${d.commits.map((c) => `<tr><td>${esc(c)}</td></tr>`).join("")}</table>`;
    if (d.files.length) {
      h += `<h3>変更ファイル（${d.files.length}）</h3><table>` + d.files.map((f) =>
        `<tr><td class="add">${f.added ?? "bin"}+</td><td class="del">${f.deleted ?? ""}−</td><td>${esc(f.path)}</td></tr>`).join("") + "</table>";
    }
    if (d.untracked.length) h += `<h3>新規ファイル（未追跡・${d.untracked.length}）</h3><table>${d.untracked.map((f) => `<tr><td class="add">new</td><td>${esc(f)}</td></tr>`).join("")}</table>`;
    if (d.stat) h += `<h3>git diff --stat</h3><div class="patch">${esc(d.stat)}</div>`;
    if (d.patch) {
      const lines = d.patch.split("\n").map((l) => {
        const cls = l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : "";
        return cls ? `<span class="${cls}">${esc(l)}</span>` : esc(l);
      });
      h += `<h3>差分</h3><div class="patch">${lines.join("\n")}</div>`;
    }
    pane.innerHTML = h;
  } catch (e) { pane.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
  $("#logPane").classList.toggle("hidden", tab !== "log");
  $("#diffPane").classList.toggle("hidden", tab !== "diff");
  if (tab === "diff") loadDiff();
}

// ---- 並列表示 -------------------------------------------------------------------
async function renderGrid() {
  const shown = sortedSessions().filter((s) => s.status !== "done");
  const grid = $("#grid");
  if (!shown.length) { grid.innerHTML = `<div class="grid-empty">表示するセッションがありません（完了以外を並べて表示します）</div>`; return; }
  for (const s of shown) if (!state.details.has(s.id)) await loadDetail(s.id).catch(() => {});
  grid.innerHTML = shown.map((s) => `<div class="gcell s-${s.status}" data-id="${s.id}">
      <div class="ghead" data-open="${s.id}"><span class="name">${esc(s.name)}</span>${badge(s.status)}</div>
      <div class="log">${gridLog(s)}</div>
      <form class="gin" data-id="${s.id}"><input placeholder="${s.status === "running" ? "実行中…" : "追加の指示"}" ${s.status === "running" ? "disabled" : ""}><button class="btn" ${s.status === "running" ? "disabled" : ""}>送信</button></form>
    </div>`).join("");
  grid.querySelectorAll(".gcell .log").forEach((l) => (l.scrollTop = l.scrollHeight));
}

function gridLog(s) {
  const turns = state.details.get(s.id)?.turns || [];
  const last = turns.slice(-2);
  return last.map((t, i) => renderTurn(t, turns.length - last.length + i)).join("") + (s.status === "running" ? `<div class="working">作業中…</div>` : "");
}

function updateGridCell(id) {
  const cell = document.querySelector(`.gcell[data-id="${id}"]`);
  const s = state.sessions.get(id);
  if (!cell || !s) { if (state.view === "grid") renderGrid(); return; }
  cell.className = `gcell s-${s.status}`;
  $(".ghead", cell).innerHTML = `<span class="name">${esc(s.name)}</span>${badge(s.status)}`;
  const running = s.status === "running";
  $(".gin input", cell).disabled = running;
  $(".gin input", cell).placeholder = running ? "実行中…" : "追加の指示";
  $(".gin button", cell).disabled = running;
  const log = $(".log", cell);
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.innerHTML = gridLog(s);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function setView(v) {
  state.view = v;
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("#detailView").classList.toggle("hidden", v !== "detail");
  $("#gridView").classList.toggle("hidden", v !== "grid");
  if (v === "grid") renderGrid();
}

// ---- 通知 ---------------------------------------------------------------------
function updateNotifyBtn() {
  const b = $("#notifyBtn");
  if (!("Notification" in window)) { b.classList.add("hidden"); return; }
  b.classList.toggle("hidden", Notification.permission === "granted");
  b.disabled = Notification.permission === "denied";
  if (Notification.permission === "denied") b.textContent = "🔕 通知はブラウザでブロック中";
}

function notify(s, prev) {
  if (prev !== "running") return;
  if (!["waiting", "error"].includes(s.status)) return;
  const title = s.status === "waiting" ? `入力待ち: ${s.name}` : `エラー: ${s.name}`;
  toast(title);
  if ("Notification" in window && Notification.permission === "granted" && (document.hidden || state.selected !== s.id)) {
    const n = new Notification(title, { body: s.last_message || "", tag: `agentdash-${s.id}` });
    n.onclick = () => { window.focus(); select(s.id); n.close(); };
  }
}

// ---- SSE ----------------------------------------------------------------------
function connect() {
  const es = new EventSource(`${API}/stream`);
  es.onopen = () => { $("#conn").classList.remove("off"); $("#conn").title = "接続中"; refreshAll(); };
  es.onerror = () => { $("#conn").classList.add("off"); $("#conn").title = "切断（自動で再接続します）"; };
  es.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.kind === "session") {
      const s = msg.session;
      const prev = state.sessions.get(s.id)?.status;
      state.sessions.set(s.id, s);
      notify(s, prev);
      renderList();
      if (s.id === state.selected) { renderDetail(); if (prev === "running" && s.status !== "running") (state.tab === "diff" ? loadDiff() : refreshDiffCount()); }
      if (state.view === "grid") updateGridCell(s.id);
    } else if (msg.kind === "turn") {
      const d = state.details.get(msg.session_id);
      if (d && !d.turns.some((t) => t.id === msg.turn.id)) d.turns.push(msg.turn);
      redraw(msg.session_id);
    } else if (msg.kind === "event") {
      const d = state.details.get(msg.session_id);
      const t = d?.turns.find((x) => x.id === msg.event.turn_id);
      if (t && !t.events.some((e) => e.id === msg.event.id)) t.events.push(msg.event);
      redraw(msg.session_id);
    } else if (msg.kind === "deleted") {
      state.sessions.delete(msg.session_id);
      state.details.delete(msg.session_id);
      if (state.selected === msg.session_id) state.selected = null;
      renderList(); renderDetail();
      if (state.view === "grid") renderGrid();
    }
  };
}

let redrawQueued = new Set();
function redraw(id) {  // イベントが連続しても1フレームに1回だけ描画
  redrawQueued.add(id);
  if (redraw._r) return;
  redraw._r = requestAnimationFrame(() => {
    redraw._r = null;
    for (const sid of redrawQueued) {
      if (sid === state.selected && state.view === "detail") renderDetail();
      if (state.view === "grid") updateGridCell(sid);
    }
    redrawQueued.clear();
  });
}

async function refreshAll() {
  const { sessions } = await api("/sessions");
  const ids = new Set(sessions.map((s) => s.id));
  state.sessions = new Map(sessions.map((s) => [s.id, s]));
  for (const id of [...state.details.keys()]) if (!ids.has(id)) state.details.delete(id);
  // 再接続時は取りこぼしがあり得るので、開いている詳細は取り直す
  for (const id of state.details.keys()) await loadDetail(id).catch(() => {});
  if (state.selected && !ids.has(state.selected)) state.selected = null;
  if (state.selected == null) {
    const first = sortedSessions()[0];
    if (first) { await select(first.id); return; }
  }
  renderList(); renderDetail();
  if (state.view === "grid") renderGrid();
}

// ---- 新規作成 -------------------------------------------------------------------
async function openNew() {
  const f = $("#newForm");
  $("#newErr").textContent = "";
  try { state.meta = await api("/meta"); } catch (e) { toast(e.message); }
  const m = state.meta || { adapters: [], defaults: {} };
  $("#adapterSel").innerHTML = m.adapters.map((a) =>
    `<option value="${esc(a.name)}" ${a.name === m.default_adapter ? "selected" : ""}>${esc(a.label)}${a.ok ? "" : "（利用不可）"}</option>`).join("");
  $("#adapterSel").title = m.adapters.map((a) => `${a.label}: ${a.detail}`).join("\n");
  updateAdapterInfo();
  $("#repoList").innerHTML = (m.recent_repos || []).map((r) => `<option value="${esc(r)}">`).join("");
  if (!f.repo_path.value && m.recent_repos?.length) f.repo_path.value = m.recent_repos[0];
  if (!f.allowed_tools.value) f.allowed_tools.value = (m.defaults.allowed_tools || []).join(", ");
  if (m.defaults.permission_mode) f.permission_mode.value = m.defaults.permission_mode;
  f.use_worktree.checked = m.defaults.use_worktree !== false;
  $("#toolPresets").innerHTML = TOOL_PRESETS.map((t) => `<button type="button" data-tool="${esc(t)}">+ ${esc(t)}</button>`).join("");
  $("#newDlg").showModal();
  (f.repo_path.value ? f.prompt : f.repo_path).focus();
}

function updateAdapterInfo() {
  const a = (state.meta?.adapters || []).find((x) => x.name === $("#adapterSel").value);
  const usesModel = a && "models" in a;
  $("#modelRow").classList.toggle("hidden", !usesModel);
  if (!a) return;
  $("#adapterInfo").textContent = (a.ok ? "✓ " : "✗ ") + a.detail;
  $("#adapterInfo").style.color = a.ok ? "" : "var(--error)";
  $("#modelList").innerHTML = (a.models || []).map((x) => `<option value="${esc(x)}">`).join("");
  $("#newForm").model.placeholder = a.model ? `既定: ${a.model}` : "";
}

async function submitNew(ev) {
  ev.preventDefault();
  const f = $("#newForm");
  const btn = $("#createBtn");
  btn.disabled = true;
  $("#newErr").textContent = "";
  try {
    const { session } = await api("/sessions", { method: "POST", body: {
      name: f.name.value, repo_path: f.repo_path.value.trim(), prompt: f.prompt.value,
      adapter: f.adapter.value, permission_mode: f.permission_mode.value,
      allowed_tools: f.allowed_tools.value, use_worktree: f.use_worktree.checked, model: f.model.value.trim(),
    } });
    $("#newDlg").close();
    f.name.value = ""; f.prompt.value = "";
    state.sessions.set(session.id, session);
    state.details.set(session.id, { turns: [] });
    await loadDetail(session.id);
    select(session.id);
  } catch (e) {
    $("#newErr").textContent = e.message;
  } finally { btn.disabled = false; }
}

// ---- イベント登録 ----------------------------------------------------------------
async function sendPrompt(id, text) {
  if (!text.trim()) return false;
  try { await api(`/sessions/${id}/messages`, { method: "POST", body: { prompt: text } }); return true; }
  catch (e) { toast(e.message); return false; }
}

function bind() {
  $("#list").addEventListener("click", (e) => { const c = e.target.closest(".card"); if (c) select(Number(c.dataset.id)); });
  $("#search").addEventListener("input", () => { renderList(); if (state.view === "grid") renderGrid(); });
  $("#hideDone").addEventListener("change", () => { renderList(); if (state.view === "grid") renderGrid(); });
  document.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("#showTools").addEventListener("change", (e) => $("#logPane").classList.toggle("hide-tools", !e.target.checked));
  $("#newBtn").addEventListener("click", openNew);
  $("#adapterSel").addEventListener("change", updateAdapterInfo);
  $("#newForm").addEventListener("submit", (e) => { if (e.submitter?.value === "cancel") { $("#newDlg").close(); e.preventDefault(); return; } submitNew(e); });
  $("#toolPresets").addEventListener("click", (e) => {
    const t = e.target.dataset.tool; if (!t) return;
    const ta = $("#newForm").allowed_tools;
    const cur = ta.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (!cur.includes(t)) ta.value = [...cur, t].join(", ");
  });
  $("#composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ta = $("#prompt");
    if (await sendPrompt(state.selected, ta.value)) ta.value = "";
  });
  $("#prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#stopBtn").addEventListener("click", () => api(`/sessions/${state.selected}/stop`, { method: "POST" }).catch((e) => toast(e.message)));
  $("#doneBtn").addEventListener("click", () => api(`/sessions/${state.selected}/complete`, { method: "POST" }).catch((e) => toast(e.message)));
  $("#delBtn").addEventListener("click", async () => {
    const s = state.sessions.get(state.selected);
    if (!s) return;
    const wt = s.worktree_path !== s.repo_path;
    if (!confirm(`「${s.name}」を削除しますか？\nログは消えます。${wt ? "\nworktree フォルダも削除します（ブランチ " + s.branch + " は残ります）。" : ""}`)) return;
    api(`/sessions/${s.id}`, { method: "DELETE" }).catch((e) => toast(e.message));
  });
  $("#dMeta").addEventListener("click", (e) => {
    const v = e.target.dataset.copy;
    if (v) navigator.clipboard?.writeText(v).then(() => toast("コピーしました"), () => {});
  });
  $("#grid").addEventListener("click", (e) => { const h = e.target.closest("[data-open]"); if (h) select(Number(h.dataset.open)); });
  $("#grid").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("input", e.target);
    if (await sendPrompt(Number(e.target.dataset.id), input.value)) input.value = "";
  });
  $("#notifyBtn").addEventListener("click", async () => { await Notification.requestPermission(); updateNotifyBtn(); });
  setInterval(() => renderList(), 30000); // 「◯分前」の更新
}

bind();
updateNotifyBtn();
connect();
