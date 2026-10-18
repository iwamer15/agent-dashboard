"""git worktree によるセッションごとの作業分離と、変更内容の取得。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows で黒い窓を出さない


def git(args: list, cwd, check=True, timeout=60) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {(p.stderr or p.stdout).strip()}")
    return p.stdout


def is_git_repo(path) -> bool:
    try:
        return git(["rev-parse", "--is-inside-work-tree"], path, check=False).strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def repo_root(path) -> Path:
    return Path(git(["rev-parse", "--show-toplevel"], path).strip())


def head_commit(path) -> str | None:
    out = git(["rev-parse", "HEAD"], path, check=False).strip()
    return out if re.fullmatch(r"[0-9a-f]{40}", out) else None


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()
    return s[:30] or "session"


def create(repo_path, worktrees_dir, session_id: int, name: str) -> dict:
    """新しいブランチで worktree を作る。コミットが1つも無いリポジトリは worktree を作れないのでエラー。"""
    root = repo_root(repo_path)
    base = head_commit(root)
    if not base:
        raise RuntimeError("コミットが無いリポジトリには worktree を作れません（先に1回コミットしてください）")
    branch = f"agentdash/{session_id}-{slug(name)}"
    path = Path(worktrees_dir) / f"{root.name}-{session_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path), base], root)
    return {"worktree_path": str(path), "branch": branch, "base_commit": base}


def remove(repo_path, worktree_path, branch: str | None = None, delete_branch=False):
    git(["worktree", "remove", "--force", str(worktree_path)], repo_path, check=False)
    if delete_branch and branch:
        git(["branch", "-D", branch], repo_path, check=False)


def diff(workdir, base_commit: str | None, full: bool = False) -> dict:
    """base_commit (セッション開始時点) からの変更。エージェントのコミット分も含む。"""
    if not is_git_repo(workdir):
        return {"git": False, "stat": "", "files": [], "untracked": [], "patch": ""}
    ref = base_commit or ("HEAD" if head_commit(workdir) else None)
    files, stat, patch = [], "", ""
    if ref:
        stat = git(["diff", "--stat", ref], workdir, check=False)
        for line in git(["diff", "--numstat", ref], workdir, check=False).splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, f = parts
                files.append({"path": f, "added": None if a == "-" else int(a),
                              "deleted": None if d == "-" else int(d)})
        if full:
            patch = git(["diff", ref], workdir, check=False)
            if len(patch) > 400_000:
                patch = patch[:400_000] + "\n…（以降省略）"
    untracked = git(["ls-files", "--others", "--exclude-standard"], workdir, check=False).splitlines()
    commits = git(["log", "--oneline", f"{ref}..HEAD"], workdir, check=False).splitlines() if ref else []
    return {"git": True, "stat": stat, "files": files, "untracked": untracked[:500], "patch": patch,
            "commits": commits[:100]}
