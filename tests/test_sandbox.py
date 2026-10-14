import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from duet.sandbox import SandboxError, TestRunner, _copy_worktree, _environment


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "test.py").write_text("print('passed')\n")

    def test_local_requires_explicit_trust(self):
        with self.assertRaises(SandboxError):
            TestRunner(mode="local")

    def test_docker_absence_fails_closed(self):
        with patch("duet.sandbox.shutil.which", return_value=None):
            with self.assertRaisesRegex(SandboxError, "no local fallback"):
                TestRunner().run(self.source)

    def test_docker_security_flags_and_no_host_secrets(self):
        runner = TestRunner()
        command = runner._docker_command(self.source, "duet-test-example")
        for required in ["--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534", "--pids-limit=64", "--memory=256m", "--cpus=1", "--pull=never"]:
            self.assertIn(required, command)
        self.assertIn(f"type=bind,source={self.source},target=/workspace,readonly", command)
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("--privileged", command)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "do-not-copy", "ANTHROPIC_API_KEY": "do-not-copy", "PYTHONPATH": "untrusted"}):
            environment = _environment()
            for name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH"]:
                self.assertNotIn(name, environment)

    def test_run_uses_staging_and_cleans_container(self):
        with patch("duet.sandbox.shutil.which", return_value="docker"), patch.object(TestRunner, "_execute", return_value={"passed": True, "returncode": 0, "output": "ok"}) as execute, patch("duet.sandbox.subprocess.run") as cleanup:
            result = TestRunner().run(self.source)
        self.assertTrue(result["passed"])
        command, staging = execute.call_args.args
        self.assertNotEqual(staging, self.source)
        self.assertFalse(staging.exists())
        self.assertEqual(cleanup.call_args.args[0][:3], ["docker", "rm", "--force"])

    def test_staging_excludes_git_and_credentials(self):
        (self.source / ".git").write_text("gitdir: sensitive-host-path")
        (self.source / ".env").write_text("API_KEY=secret")
        destination = self.root / "copy"
        _copy_worktree(self.source, destination)
        self.assertFalse((destination / ".git").exists())
        self.assertFalse((destination / ".env").exists())
        self.assertTrue((destination / "test.py").exists())

    def test_staging_rejects_secret_inside_regular_source(self):
        (self.source / "bad.py").write_text("sk-" + "a" * 30)
        with self.assertRaises(SandboxError):
            _copy_worktree(self.source, self.root / "copy")

    def test_trusted_local_executes_copy_and_bounds_output(self):
        code = "import os; from pathlib import Path; Path('artifact.txt').write_text('created'); print('x' * 20000); assert 'OPENAI_API_KEY' not in os.environ"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-inherit"}):
            result = TestRunner(mode="local", allow_local=True, command=[sys.executable, "-c", code]).run(self.source)
        self.assertTrue(result["passed"])
        self.assertIn("[output truncated]", result["output"])
        self.assertLess(len(result["output"]), 17000)
        self.assertFalse((self.source / "artifact.txt").exists())

    def test_timeout_kills_test_process(self):
        result = TestRunner(mode="local", allow_local=True, timeout=1, command=[sys.executable, "-c", "import time; time.sleep(30)"]).run(self.source)
        self.assertFalse(result["passed"])
        self.assertEqual(result["returncode"], 124)
        self.assertIn("[test timeout]", result["output"])

    @unittest.skipUnless(os.name == "nt", "Windows taskkill fallback only")
    def test_failed_taskkill_still_reaps_child_and_preserves_timeout(self):
        original_popen = subprocess.Popen
        for failure in [subprocess.TimeoutExpired(["taskkill"], 10),
                        FileNotFoundError("Synthetic missing taskkill")]:
            with self.subTest(failure=type(failure).__name__):
                children = []

                def start(*args, **kwargs):
                    child = original_popen(*args, **kwargs)
                    children.append(child)
                    return child

                runner = TestRunner(mode="local", allow_local=True, timeout=1)
                command = [sys.executable, "-c", "import time; time.sleep(30)"]
                try:
                    with patch("duet.sandbox.subprocess.Popen", side_effect=start), \
                            patch("duet.sandbox.subprocess.run", side_effect=failure) as taskkill:
                        result = runner._execute(command, self.source)
                    self.assertFalse(result["passed"])
                    self.assertEqual(result["returncode"], 124)
                    self.assertIn("[test timeout]", result["output"])
                    taskkill.assert_called_once()
                    self.assertEqual(taskkill.call_args.args[0][:2], ["taskkill", "/PID"])
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                    self.assertTrue(children[0].stdout.closed)
                    # Windows permits removing the process working directory only
                    # after its handle is released. A rename exercises that too.
                    released = self.root / "released"
                    self.source.rename(released)
                    released.rename(self.source)
                finally:
                    # Keep regression failures from leaving this synthetic child alive.
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                            child.wait(timeout=10)
