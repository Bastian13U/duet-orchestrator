"""Constrained text edits and persistent, isolated Git worktrees.

Models never supply Git arguments. Worktrees are deliberately retained for review.
This module is not a defense against a hostile process concurrently modifying disk.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any


class WorkspaceError(RuntimeError):
    pass


class PolicyError(RuntimeError):
    pass


_RESERVED = {".git", ".github", ".gitlab", ".duet", ".ssh", ".aws", ".azure", ".config", ".gnupg", "node_modules", "__pycache__", ".venv", "venv"}
_SECRET_NAMES = {".env", "credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc", ".gitconfig", ".gitmodules", ".gitattributes"}
_DEVICE = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)", re.I)
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"(?im:^\s*(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET_KEY|PASSWORD|CLIENT_SECRET))\s*[:=]\s*[\"']?[^\s\"']{8,})"
)


def contains_secret(text: str) -> bool:
    """Conservative common-secret detector; not a complete secret scanner."""
    return _SECRET.search(text) is not None


def reject_secrets(text: str) -> None:
    if contains_secret(text):
        raise PolicyError("Potential secret detected; value withheld")


def _check_link(path: Path) -> None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
        raise PolicyError("Symlinks and Windows reparse points are forbidden")


def _check_ancestors(path: Path) -> None:
    for entry in (*reversed(path.parents), path):
        _check_link(entry)


def _relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise PolicyError("Expected a bounded relative file path")
    if "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise PolicyError("Path contains forbidden characters")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(value).is_absolute():
        raise PolicyError("Absolute paths and traversal are forbidden")
    for part in parts:
        folded = part.casefold()
        if part.endswith((".", " ")) or any(c in part for c in '*?<>|"') or _DEVICE.match(part):
            raise PolicyError("Unsafe Windows filename")
        if folded in _RESERVED or folded in _SECRET_NAMES or folded.startswith(".env.") or folded.endswith((".pem", ".key", ".p12", ".pfx")):
            raise PolicyError("Protected configuration or credential path")
    return value


class Policy:
    def __init__(self, allowed_paths: list[str], max_file_bytes: int = 65536, max_total_bytes: int = 262144):
        if not isinstance(allowed_paths, list) or not allowed_paths:
            raise PolicyError("A nonempty exact file allowlist is required")
        if type(max_file_bytes) is not int or type(max_total_bytes) is not int or max_file_bytes < 1 or max_total_bytes < max_file_bytes:
            raise PolicyError("Invalid file size limits")
        self.allowed_paths = tuple(_relative_name(p) for p in allowed_paths)
        folded = [p.casefold() for p in self.allowed_paths]
        if len(folded) != len(set(folded)):
            raise PolicyError("Duplicate or case-colliding allowlist paths")
        if any(a.startswith(b + "/") for a in folded for b in folded if a != b):
            raise PolicyError("An allowlisted file cannot also be a parent directory")
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def _path(self, root: Path, value: str) -> Path:
        name = _relative_name(value)
        if name not in self.allowed_paths:
            raise PolicyError("Requested path is outside the exact allowlist")
        root = Path(root).absolute()
        _check_ancestors(root)
        candidate = root.joinpath(*name.split("/"))
        _check_ancestors(candidate)
        # Detect collisions even on a case-sensitive filesystem, so behavior is portable.
        current = root
        for segment in name.split("/"):
            if current.exists():
                if not current.is_dir():
                    raise PolicyError("A path ancestor is not a directory")
                for child in current.iterdir():
                    if child.name.casefold() == segment.casefold() and child.name != segment:
                        raise PolicyError("Path collides with differently cased existing entry")
            current = current / segment
        if candidate.exists() and not candidate.is_file():
            raise PolicyError("Target is not a regular file")
        return candidate

    def _text(self, content: Any) -> bytes:
        if not isinstance(content, str) or "\x00" in content:
            raise PolicyError("Only UTF-8 text is allowed")
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise PolicyError("Invalid UTF-8 text") from exc
        if len(data) > self.max_file_bytes:
            raise PolicyError("File exceeds configured size limit")
        if contains_secret(content):
            raise PolicyError("Potential secret detected; values withheld")
        return data

    def read_context(self, root: Path) -> dict[str, str]:
        context: dict[str, str] = {}
        total = 0
        for name in self.allowed_paths:
            path = self._path(root, name)
            if not path.exists():
                continue
            if path.stat().st_size > self.max_file_bytes:
                raise PolicyError("File exceeds configured size limit")
            try:
                content = path.read_bytes().decode("utf-8")
            except UnicodeError as exc:
                raise PolicyError("Only UTF-8 text files are allowed") from exc
            data = self._text(content)
            total += len(data)
            if total > self.max_total_bytes:
                raise PolicyError("Context exceeds configured total size limit")
            context[name] = content
        return context

    def apply(self, root: Path, changes: list[dict[str, str]]) -> None:
        if not isinstance(changes, list) or not changes:
            raise PolicyError("Expected a nonempty list of text changes")
        prepared: list[tuple[Path, bytes]] = []
        names: set[str] = set()
        total = 0
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "content"}:
                raise PolicyError("Each change requires exactly path and content")
            path = self._path(root, change["path"])
            folded = change["path"].casefold()
            if folded in names:
                raise PolicyError("Duplicate or case-colliding changes")
            names.add(folded)
            data = self._text(change["content"])
            total += len(data)
            if total > self.max_total_bytes:
                raise PolicyError("Changes exceed configured total size limit")
            prepared.append((path, data))
        # All paths and content are validated before any write occurs.
        for path, data in prepared:
            path.parent.mkdir(parents=True, exist_ok=True)
            _check_ancestors(path)
            with path.open("wb") as output:
                output.write(data)

    def digest(self, root: Path) -> str:
        checksum = hashlib.sha256()
        for name, content in sorted(self.read_context(root).items()):
            checksum.update(name.encode("utf-8") + b"\x00" + content.encode("utf-8") + b"\x00")
        return checksum.hexdigest()


def _git_env() -> dict[str, str]:
    # No caller-controlled GIT_* variables or credential-helper process state.
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT"}
    env = {key: val for key, val in os.environ.items() if key.upper() in keep}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"})
    return env


class Workspace:
    def __init__(self, repo: Path, state_dir: Path, run_id: str):
        if not isinstance(run_id, str) or not _IDENTIFIER.fullmatch(run_id):
            raise WorkspaceError("Invalid run identifier")
        self.repo = Path(repo).absolute()
        self.state_dir = Path(state_dir).absolute()
        _check_ancestors(self.repo)
        _check_ancestors(self.state_dir)
        if self.state_dir == self.repo or self.repo in self.state_dir.parents:
            raise WorkspaceError("State directory must be outside the target repository")
        self.run_id = run_id
        self.run_dir = self.state_dir / run_id
        self.integration_path = self.run_dir / "integration"
        self.branch = f"duet/{run_id}/integration"
        self._tasks: dict[str, Path] = {}
        self._extra: list[str] = []

    def _git(self, path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.autocrlf=input", "-c", "core.safecrlf=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "user.name=Duet Orchestrator", "-c", "user.email=duet@localhost", *self._extra, "-C", str(path), *args]
        try:
            result = subprocess.run(command, env=_git_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorkspaceError("Git is unavailable or timed out") from exc
        if check and result.returncode:
            # Repository content/config may include secrets; never surface arbitrary Git output.
            raise WorkspaceError(f"Git operation failed ({args[0] if args else 'unknown'}, exit {result.returncode})")
        return result

    def create(self) -> Path:
        if not self.repo.is_dir():
            raise WorkspaceError("Target repository does not exist")
        top = self._git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != self.repo.resolve():
            raise WorkspaceError("Target must be the repository root")
        # Disable repository-defined executable content filters before checkout/add.
        filters = self._git(self.repo, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$", check=False)
        for name in filters.stdout.splitlines():
            self._extra.extend(["-c", f"{name}={'false' if name.endswith('.required') else ''}"])
        if self._git(self.repo, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise WorkspaceError("Target repository must be clean, including untracked files")
        tracked = self._git(self.repo, "ls-files", "--stage", "-z").stdout
        if any(entry.startswith(("120000 ", "160000 ")) for entry in tracked.split("\x00")):
            raise WorkspaceError("Tracked symlinks and submodules are forbidden")
        if (self.repo / ".gitmodules").exists():
            raise WorkspaceError("Repositories with submodule configuration are forbidden")
        base = self._git(self.repo, "rev-parse", "--verify", "HEAD").stdout.strip()
        existing = self._git(self.repo, "for-each-ref", "--format=%(refname)", f"refs/heads/duet/{self.run_id}/").stdout.strip()
        if existing or self.run_dir.exists():
            raise WorkspaceError("Run identifier already exists; refusing to overwrite")
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self._git(self.repo, "worktree", "add", "-b", self.branch, str(self.integration_path), base)
        return self.integration_path

    def task_worktree(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id) or task_id == "integration":
            raise WorkspaceError("Invalid task identifier")
        if not self.integration_path.is_dir():
            raise WorkspaceError("Create the integration worktree first")
        path = self.run_dir / task_id
        if path.exists() or task_id in self._tasks:
            raise WorkspaceError("Task worktree already exists")
        self._git(self.repo, "worktree", "add", "-b", f"duet/{self.run_id}/{task_id}", str(path), self.head())
        self._tasks[task_id] = path
        return path

    def commit_task(self, path: Path, task_id: str) -> str:
        if task_id not in self._tasks or Path(path).absolute() != self._tasks[task_id]:
            raise WorkspaceError("Unknown task worktree")
        # Index additions are constrained upstream by Policy and the trusted runner.
        self._git(path, "add", "--all", "--", ".")
        self._git(path, "commit", "--no-verify", "-m", f"duet: complete task {task_id}")
        return self._git(path, "rev-parse", "HEAD").stdout.strip()

    def integrate(self, commit: str) -> None:
        if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
            raise WorkspaceError("Expected a full Git commit hash")
        known = {self._git(path, "rev-parse", "HEAD").stdout.strip() for path in self._tasks.values()}
        if commit not in known:
            raise WorkspaceError("Can only integrate a commit from this run's task worktrees")
        if self._git(self.integration_path, "status", "--porcelain").stdout.strip():
            raise WorkspaceError("Integration worktree is dirty")
        result = self._git(self.integration_path, "cherry-pick", commit, check=False)
        if result.returncode:
            self._git(self.integration_path, "cherry-pick", "--abort", check=False)
            raise WorkspaceError("Task integration failed; worktrees retained for inspection")

    def head(self) -> str:
        return self._git(self.integration_path, "rev-parse", "HEAD").stdout.strip()
