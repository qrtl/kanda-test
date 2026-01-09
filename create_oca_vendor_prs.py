#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml  # pip install pyyaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    # Enable next line if you want to show commands
    # print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)


def run_check(cmd: list[str], cwd: Path | None = None) -> str:
    p = run(cmd, cwd=cwd)
    if p.returncode != 0:
        msg = f"Command failed: {' '.join(cmd)}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}"
        raise RuntimeError(msg)
    return p.stdout.strip()


@dataclass
class Target:
    owner: str
    repo: str
    base_branch: str


@dataclass
class GitHubUser:
    username: str
    email: str


@dataclass
class ModuleSpec:
    oca_repo: str
    oca_branch: str
    module: str


def ticket_url(ticket_id: int) -> str:
    return (
        f"https://www.quartile.co/web#id={ticket_id}"
        f"&menu_id=505&cids=3&action=1457&model=project.task&view_type=form"
    )


def ensure_repo_cloned(workdir: Path, target: Target) -> Path:
    repo_dir = workdir / target.repo
    if repo_dir.exists():
        # If it already exists, fetch
        run_check(["git", "fetch", "origin"], cwd=repo_dir)
        return repo_dir

    run_check(["gh", "repo", "clone", f"{target.owner}/{target.repo}", str(repo_dir)])
    return repo_dir


def git_checkout_base(repo_dir: Path, base_branch: str) -> None:
    run_check(["git", "checkout", base_branch], cwd=repo_dir)
    run_check(["git", "pull", "--ff-only", "origin", base_branch], cwd=repo_dir)


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    p = run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir)
    return p.returncode == 0 and p.stdout.strip() != ""


def local_branch_exists(repo_dir: Path, branch: str) -> bool:
    p = run(["git", "show-ref", "--verify", f"refs/heads/{branch}"], cwd=repo_dir)
    return p.returncode == 0


def pr_exists_for_branch(repo_dir: Path, branch: str) -> str | None:
    # Return URL if existing PR exists (safety measure, not in requirements)
    # Treat as None even if it fails
    p = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], cwd=repo_dir)
    if p.returncode == 0:
        url = p.stdout.strip()
        if url:
            return url
    return None


def sanitize_branch(s: str) -> str:
    # Just in case. Module names are usually snake_case so mostly pass through.
    safe = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch.lower())
        else:
            safe.append("-")
    out = "".join(safe)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def vendor_copy_module(repo_dir: Path, spec: ModuleSpec) -> None:
    with tempfile.TemporaryDirectory(prefix="oca-src-") as td:
        src_dir = Path(td) / spec.oca_repo
        # Shallow clone OCA/<repo>
        run_check(
            ["gh", "repo", "clone", f"OCA/{spec.oca_repo}", str(src_dir), "--", "--depth", "1", "--branch", spec.oca_branch]
        )

        src_module_dir = src_dir / spec.module
        if not src_module_dir.is_dir():
            raise RuntimeError(
                f"Module directory not found in OCA/{spec.oca_repo}({spec.oca_branch}): {src_module_dir}"
            )

        dst_module_dir = repo_dir / spec.module
        # Remove existing before copy (assuming existence check on base_branch and skip before entering this function)
        if dst_module_dir.exists():
            shutil.rmtree(dst_module_dir)

        shutil.copytree(src_module_dir, dst_module_dir)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} prgen.yml", file=sys.stderr)
        return 2

    cfg_path = Path(sys.argv[1]).resolve()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    target = Target(**cfg["target"])
    ghuser = GitHubUser(**cfg["github"])
    ticket_id = int(cfg["ticket"]["id"])
    specs = [ModuleSpec(**m) for m in cfg["modules"]]

    # Pre-check: can we use gh
    try:
        run_check(["gh", "--version"])
        run_check(["gh", "auth", "status"])
    except Exception as e:
        print("gh is not ready. Please run `gh auth login` first.", file=sys.stderr)
        print(str(e), file=sys.stderr)
        return 1

    workdir = Path.cwd() / "_oca_pr_work"
    workdir.mkdir(exist_ok=True)

    repo_dir = ensure_repo_cloned(workdir, target)

    # git user configuration (repo local)
    run_check(["git", "config", "user.name", ghuser.username], cwd=repo_dir)
    run_check(["git", "config", "user.email", ghuser.email], cwd=repo_dir)

    print(f"Target repo: {target.owner}/{target.repo} base={target.base_branch}")
    print(f"Workdir: {repo_dir}")

    for spec in specs:
        module = spec.module
        base_branch = target.base_branch
        branch = sanitize_branch(f"{base_branch}-add-{module}")
        title = f"[{ticket_id}][ADD] {module}"
        url = ticket_url(ticket_id)
        body = f"[{ticket_id}]({url})"

        print("\n" + "=" * 80)
        print(f"Module: {module}  (OCA/{spec.oca_repo}@{spec.oca_branch})")
        print(f"Branch: {branch}")
        print(f"Title : {title}")

        # Return to base
        git_checkout_base(repo_dir, base_branch)

        # Skip if same-name module already exists on base branch (as per requirements)
        if (repo_dir / module).is_dir():
            print(f"SKIP: base branch already has module dir: {module}/")
            continue

        # Error if same-name branch already exists (as per requirements)
        if local_branch_exists(repo_dir, branch) or remote_branch_exists(repo_dir, branch):
            print(f"ERROR: branch already exists: {branch}", file=sys.stderr)
            return 3

        # Just in case, if existing PR exists, display and exit (safety measure)
        existing_pr = pr_exists_for_branch(repo_dir, branch)
        if existing_pr:
            print(f"SKIP: PR already exists: {existing_pr}")
            continue

        # Create branch
        run_check(["git", "checkout", "-b", branch], cwd=repo_dir)

        # Vendor copy
        vendor_copy_module(repo_dir, spec)

        # Commit
        run_check(["git", "add", module], cwd=repo_dir)

        # Skip if nothing changed (unlikely in theory, but just in case)
        diff = run(["git", "diff", "--cached", "--name-only"], cwd=repo_dir).stdout.strip()
        if not diff:
            print("SKIP: nothing to commit.")
            git_checkout_base(repo_dir, base_branch)
            continue

        run_check(["git", "commit", "-m", f"[ADD] {module}"], cwd=repo_dir)

        # Push
        run_check(["git", "push", "-u", "origin", branch], cwd=repo_dir)

        # Create PR (body = ticket URL)
        pr_url = run_check(
            [
                "gh",
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base_branch,
                "--head",
                branch,
            ],
            cwd=repo_dir,
        )
        print(f"Created PR: {pr_url}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
