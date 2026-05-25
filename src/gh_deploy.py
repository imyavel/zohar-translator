"""GitHub Pages auto-deploy: sync deploy-tree → git push.

Used by bot.py for both auto-deploy (after article_done events, debounced)
and the manual `🌐 Push` button / `/push` command.

Pipeline (deploy_site_to_pages):
    1. Site must already be built (caller's responsibility).
    2. For each DeployTarget:
       a) Copy Translated/Site/* into target.deploy_dir, preserving .git.
       b) Write or remove `CNAME` per target.cname.
       c) Always re-assert `.nojekyll`.
       d) git add -A; if diff is empty → record changed=False and skip.
       e) git commit + git push (token-in-URL; never persisted on disk).

Token is not written to .git/config or commit metadata; it is injected
only during push, and replaced with `***` in any logged output.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CORPUS_TOOLS = Path(__file__).resolve().parents[1] / "corpus_tools"


# Files at deploy_dir root that we keep / always recreate (not from Site/).
# .git: git internals.
# .nojekyll: tells GitHub Pages "serve files as-is, no Jekyll processing".
# CNAME: written by _apply_cname per target.
# README.md: rendered by GitHub on the repo page (separate from the site).
# LICENSE: detected by GitHub for the right-hand "License" field.
PRESERVED_AT_ROOT = (".git", ".nojekyll", "CNAME", "README.md", "LICENSE")


@dataclass
class DeployTarget:
    """One git remote + worktree pair, with optional custom domain.

    Args:
        repo:       "owner/name" on GitHub.
        deploy_dir: local path of the worktree (under state/).
        cname:      if set, write `CNAME` file with this value; if None,
                    delete `CNAME` if it exists. Also implies the repo's
                    GitHub Pages config has cname set to this value (handled
                    out-of-band via the API by the bootstrap step).
    """
    repo: str
    deploy_dir: Path
    cname: Optional[str] = None


@dataclass
class DeployResult:
    repo: str = ""
    changed: bool = False
    commit_sha: Optional[str] = None
    files_changed: int = 0
    push_output: str = ""


def _run(cmd: list[str], cwd: Path, env: Optional[dict] = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess and return CompletedProcess. Captures stdout+stderr."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _git(args: list[str], cwd: Path, *, check: bool = True,
         user_name: str | None = None,
         user_email: str | None = None,
         ) -> subprocess.CompletedProcess:
    if user_name is None:
        user_name = os.environ.get("GH_COMMIT_USER") or "zohar-translator-bot"
    if user_email is None:
        user_email = os.environ.get("GH_COMMIT_EMAIL") or "zohar-translator-bot@users.noreply.github.com"
    """Run `git` with stable identity, no global config interference."""
    full = [
        "git",
        "-c", f"user.name={user_name}",
        "-c", f"user.email={user_email}",
        "-c", "core.autocrlf=false",
        "-c", "core.quotepath=false",
        *args,
    ]
    return _run(full, cwd, check=check)


def _build_site(heb_root: Path) -> str:
    """Run build_site.py, return the tail of its stdout for the commit message.

    build_site.py is responsible for both rendering HTML and rebuilding the
    pagefind search index (since the index is part of the published site and
    must stay in sync with the HTML). We only forward its summary lines here.
    """
    script = _CORPUS_TOOLS / "build_site.py"
    if not script.is_file():
        raise FileNotFoundError(f"build_site.py not found at {script}")
    proc = _run([sys.executable, str(script)], cwd=heb_root, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(
            f"build_site.py failed (rc={proc.returncode}):\n{tail}"
        )
    # Tail: "Site built ...", "Books: ...", "Chapters done: ...",
    # "Articles done: ...", "Pagefind: ..."
    return "\n".join((proc.stdout or "").strip().splitlines()[-5:])


def _copy_site_to_deploy_dir(src: Path, dst: Path) -> None:
    """Replace dst/* with src/*, preserving .git/.nojekyll/CNAME at dst root."""
    if not src.is_dir():
        raise FileNotFoundError(f"Site source missing: {src}")
    dst.mkdir(parents=True, exist_ok=True)

    # Wipe dst root contents (except preserved files/dirs).
    for child in dst.iterdir():
        if child.name in PRESERVED_AT_ROOT:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    # Copy fresh Site/ contents on top.
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    # Re-assert .nojekyll (safety: build_site.py doesn't produce it).
    nojekyll = dst / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_bytes(b"")


def _ensure_repo(deploy_dir: Path, repo: str) -> None:
    """Make sure deploy_dir is a git repo, origin points to GH HTTPS URL, and
    local main is synced to remote main.

    Token is NEVER written into .git/config: we keep the clean URL
    `https://github.com/{repo}.git` here and inject the token only at
    push time via an explicit URL argument.

    The auto-sync (fetch + reset --hard origin/main) handles cases where
    the remote was changed out-of-band — e.g. GitHub's "Delete CNAME"
    auto-commit when detaching custom domain via API, or commits made
    from another machine. Worktree is service-only (only the bot writes
    here), so any local commits not yet pushed are discarded — re-built
    on next pipeline run anyway. NEVER edit this directory by hand.
    """
    deploy_dir.mkdir(parents=True, exist_ok=True)
    git_dir = deploy_dir / ".git"
    if not git_dir.is_dir():
        _git(["init", "-b", "main"], deploy_dir)

    # Set or update origin to a clean URL.
    clean_url = f"https://github.com/{repo}.git"
    cur = _git(["remote", "get-url", "origin"], deploy_dir, check=False)
    if cur.returncode != 0:
        _git(["remote", "add", "origin", clean_url], deploy_dir)
    elif (cur.stdout or "").strip() != clean_url:
        _git(["remote", "set-url", "origin", clean_url], deploy_dir)

    # Auto-sync: fetch remote main; if it exists, reset local HEAD to it.
    # If remote is empty (fresh repo) — fetch fails silently, we proceed
    # with whatever local state is and the first push will create main.
    fetch = _git(["fetch", "--depth=1", "origin", "main"], deploy_dir, check=False)
    if fetch.returncode == 0:
        _git(["reset", "--hard", "FETCH_HEAD"], deploy_dir, check=False)


def _commit_and_push(deploy_dir: Path, repo: str, token: str,
                     commit_msg: str) -> DeployResult:
    """Stage all changes, commit (if any), push via token-in-URL.

    Returns DeployResult; if there are no changes since last commit,
    returns changed=False without committing/pushing.
    """
    _git(["add", "-A"], deploy_dir)

    # Detect if there's anything staged. `git diff --cached --quiet`
    # returns 0 when no changes, 1 when there are changes.
    diff = _git(["diff", "--cached", "--quiet"], deploy_dir, check=False)
    if diff.returncode == 0:
        return DeployResult(changed=False)

    # Count staged files for reporting.
    name_status = _git(["diff", "--cached", "--name-only"], deploy_dir)
    files_changed = sum(1 for ln in (name_status.stdout or "").splitlines() if ln.strip())

    _git(["commit", "-m", commit_msg], deploy_dir)
    sha = _git(["rev-parse", "HEAD"], deploy_dir).stdout.strip()

    push_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    push = _git(["push", push_url, "HEAD:main"], deploy_dir, check=False)

    # Self-heal on "behind" / non-fast-forward. Can happen if remote main
    # advanced between _ensure_repo's fetch and this push (another bot
    # instance, a GitHub auto-commit, or a CNAME rewrite). Re-fetch,
    # rebase our commit on top, push again. One retry.
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "")
        if _looks_like_non_ff(err):
            logger.warning(
                "[%s] push rejected (behind/non-ff) — refetching and "
                "rebasing one local commit on top of origin/main",
                repo,
            )
            fetch = _git(
                ["fetch", "--depth=1", "origin", "main"],
                deploy_dir, check=False,
            )
            if fetch.returncode == 0:
                # Cherry-pick our single new commit onto fetched HEAD.
                # Easier than rebase: hard reset to remote, then re-stage
                # all changes and recommit. The copied Site/ on disk
                # is already the desired state.
                _git(["reset", "--hard", "FETCH_HEAD"], deploy_dir, check=False)
                _git(["add", "-A"], deploy_dir)
                diff_again = _git(
                    ["diff", "--cached", "--quiet"], deploy_dir, check=False,
                )
                if diff_again.returncode == 0:
                    # Remote already had the same content — nothing to push.
                    return DeployResult(changed=False)
                _git(["commit", "-m", commit_msg], deploy_dir)
                sha = _git(["rev-parse", "HEAD"], deploy_dir).stdout.strip()
                push = _git(
                    ["push", push_url, "HEAD:main"], deploy_dir, check=False,
                )

    if push.returncode != 0:
        # Mask token in any error logs.
        masked = (push.stderr or push.stdout or "").replace(token, "***")
        raise RuntimeError(f"git push failed (rc={push.returncode}):\n{masked}")

    masked_out = (push.stdout + push.stderr).replace(token, "***")
    return DeployResult(
        changed=True,
        commit_sha=sha,
        files_changed=files_changed,
        push_output=masked_out.strip(),
    )


_NON_FF_TOKENS = (
    "non-fast-forward", "fetch first", "behind", "rejected",
)


def _looks_like_non_ff(err: str) -> bool:
    low = (err or "").lower()
    return any(t in low for t in _NON_FF_TOKENS)


def _apply_cname(deploy_dir: Path, cname: Optional[str]) -> None:
    """Write or remove CNAME at the root of the worktree per target spec."""
    cname_path = deploy_dir / "CNAME"
    if cname:
        # Write atomically with LF and no BOM.
        cname_path.write_bytes((cname.strip() + "\n").encode("utf-8"))
    else:
        if cname_path.exists():
            cname_path.unlink()


def _summarize_changes(deploy_dir: Path, max_files: int = 15) -> str:
    """Return a short summary of staged changes for the commit body.

    Calls `git diff --cached --name-only` AFTER `git add -A` was run by
    the caller. Truncates to first `max_files` paths to keep the commit
    message readable.
    """
    name_status = _git(
        ["diff", "--cached", "--name-only"], deploy_dir, check=False,
    )
    lines = [
        ln.strip() for ln in (name_status.stdout or "").splitlines() if ln.strip()
    ]
    if not lines:
        return "(no file-level diff)"
    total = len(lines)
    head = lines[:max_files]
    suffix = f"\n… and {total - max_files} more files" if total > max_files else ""
    return f"Changed files ({total}):\n  " + "\n  ".join(head) + suffix


def deploy_site_to_pages(
    heb_root: Path,
    targets: list[DeployTarget],
    token: str,
    commit_msg_prefix: str = "Auto-deploy",
) -> list[DeployResult]:
    """Pipeline: assume site is ALREADY built; for each target: copy →
    CNAME → add → commit-if-changes → push.

    Site build is the caller's responsibility (bot._run_build_site).
    Decoupling fixed a cross-process collision between three concurrent
    build_site.py invocations (mark_article_done + process_results +
    gh_deploy) that caused PermissionError on `pagefind/*.pf_index`
    and `Translated/Site/*.html`.

    Synchronous; meant to be called via `asyncio.to_thread(...)` from bot.

    Args:
        heb_root: cfg.heb_root (path to Heb/). Must contain `Translated/Site/`.
        targets: list of DeployTarget. Order matters only for log output;
            each target is independent and uses its own worktree.
        token: cfg.gh_token, GitHub PAT with `repo` scope.
        commit_msg_prefix: human prefix for commit message; the staged
            file list summary is appended per-target inside _commit_and_push.

    Returns:
        List of DeployResult, one per target, in the same order as `targets`.

    Raises:
        FileNotFoundError: if `heb_root / Translated / Site` is missing.
    """
    if not token:
        raise ValueError("token is empty — refusing to deploy")
    if not targets:
        raise ValueError("targets is empty — refusing to deploy")

    site_src = heb_root / "Translated" / "Site"
    if not site_src.is_dir():
        raise FileNotFoundError(
            f"Site source missing: {site_src}. Run build_site.py first "
            f"(or use /rebuild) — gh_deploy no longer builds the site itself."
        )

    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    results: list[DeployResult] = []
    for tgt in targets:
        _ensure_repo(tgt.deploy_dir, tgt.repo)
        _copy_site_to_deploy_dir(site_src, tgt.deploy_dir)
        _apply_cname(tgt.deploy_dir, tgt.cname)
        _git(["add", "-A"], tgt.deploy_dir)
        summary = _summarize_changes(tgt.deploy_dir)
        msg = f"{commit_msg_prefix} {ts}\n\n{summary}"
        res = _commit_and_push(tgt.deploy_dir, tgt.repo, token, msg)
        res.repo = tgt.repo
        results.append(res)
    return results


if __name__ == "__main__":
    # CLI smoke-test: python gh_deploy.py
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    heb_root = Path(os.environ["HEB_ROOT"])
    state_dir = Path(__file__).resolve().parent / "state"
    token = os.environ["GH_TOKEN"]

    targets = [
        DeployTarget(repo=os.environ["GH_REPO"],
                     deploy_dir=state_dir / "site_git", cname=None),
    ]

    results = deploy_site_to_pages(
        heb_root=heb_root, targets=targets, token=token,
        commit_msg_prefix="Manual smoke-test",
    )
    for r in results:
        print(f"[{r.repo}] changed={r.changed} sha={r.commit_sha} files={r.files_changed}")
        if r.push_output:
            print("  " + r.push_output.replace("\n", "\n  "))
