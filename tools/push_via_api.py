"""Push a local branch to GitHub through the REST API instead of git-over-HTTPS.

Why this exists: on this network `github.com:443` times out while `api.github.com:443` is
reachable, so `git push` cannot work but the API can. SSH ports are open too, but there is no
SSH key on this machine and adding one needs a human.

It replays real history rather than squashing: for each commit, oldest first, it uploads only
the blobs that changed, builds a tree on top of the parent's tree, and creates a commit with the
original message, author and committer. The result is the same history a `git push` would have
produced, which is what makes the repository useful to continue from elsewhere.

Usage:
    python tools/push_via_api.py --repo AlexYuhuFeng/TigerQuant --branch dev --remote-branch main
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"


def token() -> str:
    """The `gh` CLI's token, read from its own credential store rather than from a file."""
    result = subprocess.run(
        ["gh", "auth", "token"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    value = result.stdout.strip()
    if not value:
        msg = "no GitHub token available from `gh auth token`; run `gh auth login` first"
        raise SystemExit(msg)
    return value


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=REPO_ROOT,
    )
    return result.stdout


def api(method: str, path: str, payload: dict[str, object] | None, auth: str) -> dict[str, object]:
    """One API call, with a retry on the transient failures a long run will hit."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {auth}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "TigerQuant-push",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            if error.code in {422} and "already exists" in detail:
                return {"already_exists": True}
            if error.code in {500, 502, 503, 504} and attempt < 3:
                time.sleep(2**attempt)
                continue
            msg = f"{method} {path} -> {error.code}: {detail}"
            raise SystemExit(msg) from error
        except (TimeoutError, urllib.error.URLError) as error:
            if attempt < 3:
                time.sleep(2**attempt)
                continue
            msg = f"{method} {path} -> {type(error).__name__}: {error}"
            raise SystemExit(msg) from error
    msg = f"{method} {path} exhausted retries"
    raise SystemExit(msg)


def commits_oldest_first(branch: str) -> list[str]:
    listing = git("rev-list", "--reverse", branch)
    return [line for line in listing.splitlines() if line.strip()]


def blob_sha(path: str, repo: str, auth: str, cache: dict[str, str]) -> str:
    """Upload one file's content, reusing a cached sha for identical content."""
    content = subprocess.run(
        ["git", "show", f"{path}"],
        capture_output=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout
    import base64
    import hashlib

    key = hashlib.sha256(content).hexdigest()
    if key in cache:
        return cache[key]
    result = api(
        "POST",
        f"/repos/{repo}/git/blobs",
        {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        auth,
    )
    sha = str(result["sha"])
    cache[key] = sha
    return sha


def changed_files(commit: str) -> list[tuple[str, str]]:
    """(status, path) for the files this commit touched relative to its first parent."""
    parents = git("rev-list", "--parents", "-n", "1", commit).split()[1:]
    if not parents:
        listing = git("ls-tree", "-r", "--name-only", commit)
        return [("A", path) for path in listing.splitlines() if path.strip()]
    listing = git("diff-tree", "-r", "--name-status", "--no-commit-id", parents[0], commit)
    rows: list[tuple[str, str]] = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][:1], parts[-1]))
    return rows


def commit_metadata(commit: str) -> dict[str, str]:
    raw = git("show", "-s", "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B", commit)
    parts = raw.split("\x00", 6)
    return {
        "author_name": parts[0],
        "author_email": parts[1],
        "author_date": parts[2],
        "committer_name": parts[3],
        "committer_email": parts[4],
        "committer_date": parts[5],
        "message": parts[6].rstrip("\n"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="dev", help="local branch to replay")
    parser.add_argument("--remote-branch", default="main")
    parser.add_argument(
        "--skip-commits", type=int, default=0, help="resume: commits already pushed"
    )
    args = parser.parse_args()

    auth = token()
    history = commits_oldest_first(args.branch)[args.skip_commits :]
    print(f"replaying {len(history)} commit(s) from {args.branch} -> {args.remote_branch}")

    blobs: dict[str, str] = {}
    parent_commit: str | None = None
    parent_tree: str | None = None

    for index, commit in enumerate(history, start=1):
        entries: list[dict[str, object]] = []
        for status, path in changed_files(commit):
            if status == "D":
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            mode = git("ls-tree", commit, "--", path).split()[0]
            entries.append(
                {
                    "path": path,
                    "mode": mode,
                    "type": "blob",
                    "sha": blob_sha(f"{commit}:{path}", args.repo, auth, blobs),
                }
            )

        tree_payload: dict[str, object] = {"tree": entries}
        if parent_tree:
            tree_payload["base_tree"] = parent_tree
        tree = api("POST", f"/repos/{args.repo}/git/trees", tree_payload, auth)
        tree_sha = str(tree["sha"])

        meta = commit_metadata(commit)
        commit_payload: dict[str, object] = {
            "message": meta["message"],
            "tree": tree_sha,
            "author": {
                "name": meta["author_name"],
                "email": meta["author_email"],
                "date": meta["author_date"],
            },
            "committer": {
                "name": meta["committer_name"],
                "email": meta["committer_email"],
                "date": meta["committer_date"],
            },
        }
        if parent_commit:
            commit_payload["parents"] = [parent_commit]
        created = api("POST", f"/repos/{args.repo}/git/commits", commit_payload, auth)
        parent_commit = str(created["sha"])
        parent_tree = tree_sha
        print(
            f"  [{index}/{len(history)}] {commit[:8]} -> {parent_commit[:8]} {meta['message'].splitlines()[0][:58]}"
        )

    if parent_commit is None:
        print("nothing to push")
        return 0

    ref = f"refs/heads/{args.remote_branch}"
    existing = api("GET", f"/repos/{args.repo}/git/ref/heads/{args.remote_branch}", None, auth)
    if existing.get("already_exists"):
        existing = {}
    if "ref" in existing:
        api(
            "PATCH",
            f"/repos/{args.repo}/git/refs/heads/{args.remote_branch}",
            {"sha": parent_commit, "force": True},
            auth,
        )
        print(f"updated {args.remote_branch} -> {parent_commit[:8]}")
    else:
        api("POST", f"/repos/{args.repo}/git/refs", {"ref": ref, "sha": parent_commit}, auth)
        print(f"created {args.remote_branch} -> {parent_commit[:8]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
