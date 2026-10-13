"""Create a private GitHub repository and publish main using existing credentials.

Credentials are read from an explicitly configured environment variable or Git's
credential helper. They are never written, printed, or passed as command arguments.
The script makes no changes unless --publish is supplied.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request


class PublishError(Exception):
    """Safe, deliberately non-sensitive operator message."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublishError("GitHub returned an HTTP redirect; publication stopped.")


def git_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Never trigger an unexpected authentication UI or terminal prompt.
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    for key in list(env):
        if key.startswith(("GIT_TRACE", "GIT_CURL_VERBOSE")):
            env.pop(key, None)
    return env


def git(repo: Path, *args: str, input_text: str | None = None,
        allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], input=input_text, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=git_environment(),
            timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise PublishError("Git could not complete the operation.") from None
    if result.returncode and not allow_failure:
        raise PublishError("Git operation failed; output hidden to protect credentials.")
    return result


def credential(repo: Path, owner: str) -> tuple[str, str]:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value, "environment"
    result = git(repo, "credential", "fill",
                 input_text=f"protocol=https\nhost=github.com\nusername={owner}\n\n",
                 allow_failure=True)
    if result.returncode:
        raise PublishError("No authorized GitHub credential available. Sign in with Git Credential Manager or configure GH_TOKEN securely outside chat.")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    token = fields.get("password", "")
    if not token:
        raise PublishError("The credential helper supplied no usable GitHub credential.")
    return token, "credential-helper"


def api(token: str, path: str, payload: dict | None = None,
        allow_missing: bool = False) -> dict | None:
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "duet-orchestrator-publisher/0.1",
        },
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(2_000_001)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 404 and allow_missing:
            return None
        raise PublishError(f"GitHub request failed (HTTP {status}); response body hidden.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PublishError("GitHub connection failed; no credentials were displayed.") from None
    if len(body) > 2_000_000:
        raise PublishError("GitHub returned an unexpectedly large response.")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError):
        raise PublishError("GitHub returned an invalid response.") from None
    if not isinstance(result, dict):
        raise PublishError("GitHub returned an unexpected response type.")
    return result


def validate_remote(remote: dict, owner: str, name: str) -> None:
    if remote.get("private") is not True:
        raise PublishError("The existing repository is not private; publication stopped.")
    if str(remote.get("full_name", "")).casefold() != f"{owner}/{name}".casefold():
        raise PublishError("Repository identity differs from the explicitly requested target.")
    if remote.get("archived") or remote.get("disabled"):
        raise PublishError("The requested repository is archived or disabled.")


def validate_local(repo: Path, url: str) -> bool:
    actual = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(actual).resolve() != repo:
        raise PublishError("The selected directory must be the root of its own Git repository.")
    if git(repo, "branch", "--show-current").stdout.strip() != "main":
        raise PublishError("Check out the intended main branch before publishing.")
    git(repo, "rev-parse", "--verify", "HEAD")
    if git(repo, "status", "--porcelain").stdout.strip():
        raise PublishError("Commit or remove pending changes before publishing.")
    remotes = git(repo, "remote").stdout.splitlines()
    if "origin" not in remotes:
        return False
    fetch_urls = git(repo, "remote", "get-url", "--all", "origin").stdout.splitlines()
    push_urls = git(repo, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    # HTTPS only, exact fixed destination, no userinfo, alternate push destination,
    # or hidden token in an origin URL. Git resolves insteadOf via get-url.
    normalized = lambda urls: [value.rstrip("/").casefold() for value in urls]
    if normalized(fetch_urls) != [url.casefold()] or normalized(push_urls) != [url.casefold()]:
        raise PublishError("Origin differs from the expected GitHub HTTPS URL; no remote was changed.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Check authentication and target privacy without mutations")
    action.add_argument("--publish", action="store_true", help="Create private repository if absent and push main")
    parser.add_argument("--expected-owner", required=True)
    parser.add_argument("--name", default="duet-orchestrator")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", args.expected_owner):
        parser.error("Invalid GitHub owner")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.name):
        parser.error("Invalid repository name")
    repo = args.repo.resolve()
    url = f"https://github.com/{args.expected_owner}/{args.name}.git"
    try:
        if not repo.is_dir():
            raise PublishError("The local repository directory does not exist.")
        token, source = credential(repo, args.expected_owner)
        account = api(token, "/user")
        if str(account.get("login", "")).casefold() != args.expected_owner.casefold():
            raise PublishError("The authenticated account is not the explicitly expected GitHub owner.")
        remote = api(token, f"/repos/{args.expected_owner}/{args.name}", allow_missing=True)
        if remote is not None:
            validate_remote(remote, args.expected_owner, args.name)
        result = {"owner": args.expected_owner, "credential_source": source,
                  "url": url.removesuffix(".git"), "private": True,
                  "repository_exists": remote is not None, "published": False}
        if args.publish:
            has_origin = validate_local(repo, url)
            if remote is None:
                remote = api(token, "/user/repos", {
                    "name": args.name, "private": True, "auto_init": False,
                    "description": "Bounded OpenAI + Claude multiagent orchestrator",
                    "has_projects": False, "has_wiki": False,
                })
                validate_remote(remote, args.expected_owner, args.name)
            # Push deliberately uses the configured Git credential helper; no
            # token is put in a process argument, config, URL, or temporary file.
            if not has_origin:
                git(repo, "remote", "add", "origin", url)
            validate_local(repo, url)
            git(repo, "-c", f"core.hooksPath={os.devnull}", "-c", "push.followTags=false",
                "push", "--set-upstream", "origin", "main")
            result.update(repository_exists=True, published=True)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
