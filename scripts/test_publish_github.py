"""Offline regression checks for private repository publication safeguards."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import publish_github as publisher


OWNER = "ExampleOwner"
NAME = "duet-orchestrator"
URL = f"https://github.com/{OWNER}/{NAME}.git"


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name).resolve()

    def run_main(self, action="--check", **api_values):
        def fake_api(token, path, payload=None, allow_missing=False):
            if path == "/user":
                return {"login": api_values.get("login", OWNER)}
            if path.startswith("/repos/"):
                return api_values.get("remote")
            raise AssertionError("An unexpected mutating API call was attempted")

        out, err = io.StringIO(), io.StringIO()
        with patch.object(publisher, "credential", return_value=("SENSITIVE_TEST_TOKEN", "mock")), \
                patch.object(publisher, "api", side_effect=fake_api), \
                patch.object(publisher, "git") as git, \
                redirect_stdout(out), redirect_stderr(err):
            status = publisher.main([action, "--expected-owner", OWNER, "--repo", str(self.repo)])
        return status, out.getvalue(), err.getvalue(), git

    def test_check_is_read_only_and_reports_missing(self):
        status, out, err, git = self.run_main()
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(out)["repository_exists"])
        self.assertFalse(json.loads(out)["published"])
        self.assertEqual(err, "")
        git.assert_not_called()
        self.assertNotIn("SENSITIVE_TEST_TOKEN", out)

    def test_mismatched_account_stops_before_local_mutations(self):
        status, out, err, git = self.run_main("--publish", login="OtherOwner")
        self.assertEqual(status, 1)
        self.assertIn("not the explicitly expected", err)
        self.assertEqual(out, "")
        git.assert_not_called()

    def test_public_repository_is_never_published(self):
        status, out, err, git = self.run_main("--publish", remote={
            "private": False, "full_name": f"{OWNER}/{NAME}",
        })
        self.assertEqual(status, 1)
        self.assertIn("not private", err)
        git.assert_not_called()

    def test_remote_identity_mismatch_rejected(self):
        with self.assertRaises(publisher.PublishError):
            publisher.validate_remote({"private": True, "full_name": "other/repo"}, OWNER, NAME)

    def test_redirect_never_forwards_authorization(self):
        with self.assertRaises(publisher.PublishError):
            publisher.NoRedirect().redirect_request(None, None, 302, "found", {}, "https://other.test")

    def test_api_error_body_is_not_exposed(self):
        failure = urllib.error.HTTPError("https://api.github.com/user", 403, "SENSITIVE_BODY", {},
                                         io.BytesIO(b"SENSITIVE_TOKEN"))
        with patch.object(publisher.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(publisher.PublishError) as caught:
                publisher.api("SENSITIVE_AUTH", "/user")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("SENSITIVE", str(caught.exception))

    def test_extra_push_destinations_are_rejected(self):
        def fake_git(repo, *args, **kwargs):
            command = tuple(args)
            outputs = {
                ("rev-parse", "--show-toplevel"): str(self.repo),
                ("branch", "--show-current"): "main",
                ("rev-parse", "--verify", "HEAD"): "a" * 40,
                ("status", "--porcelain"): "",
                ("remote",): "origin",
                ("remote", "get-url", "--all", "origin"): URL,
                ("remote", "get-url", "--push", "--all", "origin"): URL + "\nhttps://other.test/leak.git",
            }
            return subprocess.CompletedProcess(args, 0, outputs[command], "")

        with patch.object(publisher, "git", side_effect=fake_git):
            with self.assertRaises(publisher.PublishError):
                publisher.validate_local(self.repo, URL)

    def test_git_failure_does_not_print_output(self):
        result = subprocess.CompletedProcess([], 1, "SENSITIVE_STDOUT", "SENSITIVE_STDERR")
        with patch.object(publisher.subprocess, "run", return_value=result):
            with self.assertRaises(publisher.PublishError) as caught:
                publisher.git(self.repo, "status")
        self.assertNotIn("SENSITIVE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
