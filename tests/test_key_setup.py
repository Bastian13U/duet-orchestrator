import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import configure_api_keys, secure_live_smoke


class SetupTests(unittest.TestCase):
    def test_noninteractive_setup_refuses_before_accessing_credentials(self):
        with patch('sys.stdin.isatty', return_value=False), \
                patch('duet.credentials.status') as status, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure_api_keys.main([]), 2)
        status.assert_not_called()

    def test_setup_preserves_openai_and_only_prompts_for_missing_claude(self):
        def current(name):
            present = name == 'OPENAI_API_KEY'
            return {'present': present, 'source': '.env.local' if present else 'missing'}
        with patch('sys.stdin.isatty', return_value=True), \
                patch('duet.credentials.status', side_effect=current), \
                patch('duet.credentials.store_key') as store, \
                patch('getpass.getpass', return_value='fake-test-key') as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(configure_api_keys.main([]), 0)
        prompt.assert_called_once()
        self.assertIn('Claude', prompt.call_args.args[0])
        store.assert_called_once_with('ANTHROPIC_API_KEY', 'fake-test-key', replace=False)
        self.assertNotIn('fake-test-key', out.getvalue())

    def test_provider_selector_does_not_consult_the_other_provider(self):
        with patch('sys.stdin.isatty', return_value=True), \
                patch('duet.credentials.status', return_value={'present': True, 'source': 'environment'}) as status, \
                patch('getpass.getpass') as prompt, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure_api_keys.main(['--provider', 'anthropic']), 0)
        status.assert_called_once_with('ANTHROPIC_API_KEY')
        prompt.assert_not_called()

    def test_replace_does_not_shadow_an_environment_or_file_key(self):
        for source in ('environment', '.env.local'):
            with self.subTest(source=source), patch('sys.stdin.isatty', return_value=True), \
                    patch('duet.credentials.status', return_value={'present': True, 'source': source}), \
                    patch('duet.credentials.store_key') as store, \
                    patch('getpass.getpass') as prompt, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(configure_api_keys.main(['--replace', '--provider', 'openai']), 2)
                prompt.assert_not_called()
                store.assert_not_called()

    def test_unavailable_source_does_not_trigger_a_replacement(self):
        with patch('sys.stdin.isatty', return_value=True), \
                patch('duet.credentials.status', return_value={'present': False, 'source': 'unavailable'}), \
                patch('getpass.getpass') as prompt, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure_api_keys.main([]), 1)
        prompt.assert_not_called()


class SmokeSetupTests(unittest.TestCase):
    def test_existing_output_stops_before_prompting_or_calling_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'orchestrator'
            root.mkdir()
            (root.parent / 'live-validation').mkdir()
            with patch.object(secure_live_smoke, 'ROOT', root), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch('duet.credentials.get_key') as resolver, \
                    patch('duet.__main__.main') as cli, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(secure_live_smoke.main(), 2)
            resolver.assert_not_called()
            cli.assert_not_called()

    def test_only_prompted_keys_are_removed_and_shared_budget_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'orchestrator'
            root.mkdir()
            with patch.object(secure_live_smoke, 'ROOT', root), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'existing-fake-key'}, clear=True), \
                    patch('duet.credentials.get_key', side_effect=lambda name: os.environ.get(name)), \
                    patch('getpass.getpass', return_value='prompted-fake-key') as prompt, \
                    patch('duet.__main__.main', return_value=0) as cli, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(secure_live_smoke.main(), 0)
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'existing-fake-key')
                self.assertNotIn('ANTHROPIC_API_KEY', os.environ)
            prompt.assert_called_once()
            arguments = cli.call_args.args[0]
            self.assertEqual(arguments[-2:], ['--state-dir', str(root.parent / '.duet-live-smoke-state')])
            self.assertNotIn('fake-key', out.getvalue())

    def test_interruption_removes_prompted_keys_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'orchestrator'
            root.mkdir()
            with patch.object(secure_live_smoke, 'ROOT', root), \
                    patch('sys.stdin.isatty', return_value=True), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch('duet.credentials.get_key', return_value=None), \
                    patch('getpass.getpass', side_effect=['fake-test-key', KeyboardInterrupt]), \
                    patch('duet.__main__.main') as cli, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(secure_live_smoke.main(), 2)
                self.assertNotIn('OPENAI_API_KEY', os.environ)
            cli.assert_not_called()
            self.assertNotIn('fake-test-key', out.getvalue())


if __name__ == '__main__':
    unittest.main()
