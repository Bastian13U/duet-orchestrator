"""Network-isolated Docker test execution with a deliberate trusted-local opt-in.

The local runner is for trusted fixtures only. It is not a security boundary.
Docker shares its host kernel; hostile tenants require a VM-level boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import uuid

from .workspace import contains_secret


class SandboxError(RuntimeError):
    pass


_OUTPUT_LIMIT = 16384
_COPY_LIMIT = 16 * 1024 * 1024
_EXCLUDED = {".git", ".env", ".ssh", ".aws", ".azure", ".gnupg", ".config", ".duet", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}


def _environment() -> dict[str, str]:
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
    env = {key: val for key, val in os.environ.items() if key.upper() in keep}
    env.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _safe_output(raw: str) -> str:
    # Secret values are withheld even when they originated in subprocess output.
    return "[output withheld: possible secret]" if contains_secret(raw) else raw


def _copy_worktree(source: Path, destination: Path) -> None:
    size = 0
    for current, dirs, files in os.walk(source, followlinks=False):
        base = Path(current)
        selected = []
        for name in dirs:
            item = base / name
            attrs = item.lstat()
            if stat.S_ISLNK(attrs.st_mode) or getattr(attrs, "st_file_attributes", 0) & 0x400:
                raise SandboxError("Test staging forbids symlinks and reparse points")
            if name.casefold() not in _EXCLUDED:
                selected.append(name)
        dirs[:] = selected
        output = destination / base.relative_to(source)
        output.mkdir(parents=True, exist_ok=True)
        for name in files:
            item = base / name
            attrs = item.lstat()
            if not stat.S_ISREG(attrs.st_mode) or getattr(attrs, "st_file_attributes", 0) & 0x400:
                raise SandboxError("Test staging requires regular files")
            folded = name.casefold()
            if folded in _EXCLUDED or folded.startswith(".env.") or folded.endswith((".pem", ".key", ".pfx", ".p12")) or folded in {"credentials", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}:
                continue
            size += attrs.st_size
            if size > _COPY_LIMIT:
                raise SandboxError("Test staging exceeds 16 MiB limit")
            data = item.read_bytes()
            if contains_secret(data.decode("utf-8", errors="replace")):
                raise SandboxError("Potential secret in test staging; values withheld")
            (output / name).write_bytes(data)
            if os.name != "nt":
                (output / name).chmod(0o755 if attrs.st_mode & 0o111 else 0o644)
        if os.name != "nt":
            output.chmod(0o755)


class TestRunner:
    __test__ = False

    def __init__(self, mode: str = "docker", command: list[str] | None = None, timeout: int = 60, image: str = "python:3.12-slim", allow_local: bool = False):
        if mode == "local-trusted":
            mode = "local"
        if mode not in {"docker", "local"}:
            raise SandboxError("Test mode must be docker or local")
        if mode == "local" and not allow_local:
            raise SandboxError("Local tests require explicit allow_local for trusted code")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise SandboxError("Test timeout must be 1..600 seconds")
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", image):
            raise SandboxError("Invalid Docker image")
        self.command = command if command is not None else ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
        if not isinstance(self.command, list) or not self.command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.command):
            raise SandboxError("Test command must be a nonempty argument list")
        self.mode, self.timeout, self.image = mode, timeout, image

    def _docker_command(self, staging: Path, name: str) -> list[str]:
        return ["docker", "run", "--rm", "--pull=never", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534", "--pids-limit=64", "--memory=256m", "--cpus=1", "--ulimit", "nofile=256:256", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777", "--mount", f"type=bind,source={staging},target=/workspace,readonly", "--workdir=/workspace", "--env", "HOME=/tmp", "--env", "PYTHONNOUSERSITE=1", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONIOENCODING=utf-8", self.image, *self.command]

    def _execute(self, args: list[str], cwd: Path) -> dict:
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            proc = subprocess.Popen(args, cwd=cwd, env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **options)
        except OSError as exc:
            raise SandboxError("Test executable is unavailable") from exc
        captured = bytearray()
        truncated = False

        def consume() -> None:
            nonlocal truncated
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                remaining = _OUTPUT_LIMIT - len(captured)
                if remaining > 0:
                    captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.kill()
            proc.wait(timeout=10)
        reader.join(timeout=2)
        if not reader.is_alive():
            proc.stdout.close()
        output = captured.decode("utf-8", errors="replace")
        if truncated:
            output += "\n[output truncated]"
        if timed_out:
            output += "\n[test timeout]"
        return {"passed": proc.returncode == 0 and not timed_out, "returncode": 124 if timed_out else proc.returncode, "output": _safe_output(output)}

    def run(self, path: Path) -> dict:
        path = Path(path).absolute()
        if not path.is_dir():
            raise SandboxError("Test worktree does not exist")
        if self.mode == "docker" and shutil.which("docker") is None:
            raise SandboxError("Docker is unavailable; no local fallback is permitted")
        with tempfile.TemporaryDirectory(prefix="duet-tests-") as temporary:
            staging = Path(temporary) / "workspace"
            staging.mkdir()
            _copy_worktree(path, staging)
            if self.mode == "local":
                return self._execute(self.command, staging)
            name = "duet-test-" + uuid.uuid4().hex
            try:
                return self._execute(self._docker_command(staging, name), staging)
            finally:
                # Killing the Docker client alone does not stop a running container.
                subprocess.run(["docker", "rm", "--force", name], env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
