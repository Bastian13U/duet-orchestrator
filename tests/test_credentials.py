import ctypes
from contextlib import ExitStack
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import MagicMock, patch

from duet import credentials


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='duet-credential-tests-'))
        self.file = Path(temporary) / '.env.local'
        self.stack.enter_context(patch.object(credentials, 'LOCAL_ENV_FILE', self.file))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.vault = self.stack.enter_context(patch.object(credentials, 'read_stored', return_value=None))
        self.native = self.stack.enter_context(patch.object(credentials, '_native', side_effect=AssertionError('Real vault access forbidden in tests')))

    def write(self, text):
        self.file.write_text(text, encoding='utf-8')

    def test_environment_precedes_file_and_vault(self):
        self.write('malformed file that must not be consulted')
        os.environ['OPENAI_API_KEY'] = 'synthetic-environment-value'
        self.assertEqual(credentials.get_key('OPENAI_API_KEY'), 'synthetic-environment-value')
        self.assertEqual(credentials.status('OPENAI_API_KEY'), {'present': True, 'source': 'environment'})
        self.vault.assert_not_called()

    def test_file_precedes_vault_and_never_copies(self):
        self.write('\ufeff# fixture only\nOPENAI_API_KEY="synthetic-file-value"\nANTHROPIC_API_KEY=\'synthetic-second-value\'\n')
        self.assertEqual(credentials.get_key('OPENAI_API_KEY'), 'synthetic-file-value')
        self.assertEqual(credentials.get_key('ANTHROPIC_API_KEY'), 'synthetic-second-value')
        self.assertEqual(credentials.status('OPENAI_API_KEY'), {'present': True, 'source': '.env.local'})
        self.vault.assert_not_called()
        self.native.assert_not_called()

    def test_missing_file_or_name_falls_back_to_vault(self):
        self.vault.return_value = 'synthetic-vault-value'
        self.assertEqual(credentials.get_key('OPENAI_API_KEY'), 'synthetic-vault-value')
        self.write('ANTHROPIC_API_KEY=synthetic-other-value\n')
        self.assertEqual(credentials.status('OPENAI_API_KEY'), {'present': True, 'source': 'windows-credential-manager'})
        self.vault.assert_called_with('OPENAI_API_KEY')

    def test_absent_value_is_reported_without_secrets(self):
        self.assertIsNone(credentials.get_key('OPENAI_API_KEY'))
        self.assertEqual(credentials.status('ANTHROPIC_API_KEY'), {'present': False, 'source': 'missing'})

    def test_file_values_are_literal_never_expanded(self):
        self.write('OPENAI_API_KEY=${SYNTHETIC_OTHER}\nANTHROPIC_API_KEY=$(synthetic-command)\n')
        self.assertEqual(credentials.get_key('OPENAI_API_KEY'), '${SYNTHETIC_OTHER}')
        self.assertEqual(credentials.get_key('ANTHROPIC_API_KEY'), '$(synthetic-command)')

    def test_invalid_file_fails_closed_with_generic_message(self):
        for text in ['OPENAI_API_KEY=synthetic-secret\nOPENAI_API_KEY=duplicate',
                     'UNKNOWN=synthetic-secret', 'export OPENAI_API_KEY=synthetic-secret',
                     'OPENAI_API_KEY', 'OPENAI_API_KEY=',
                     'OPENAI_API_KEY="synthetic-secret', "OPENAI_API_KEY=synthetic-secret'",
                     'OPENAI_API_KEY=synthetic secret', 'OPENAI_API_KEY=synthetic\x00secret',
                     'OPENAI_API_KEY=synthetic-secret\v', 'OPENAI_API_KEY=synthetic-secret\u2028',
                     'OPENAI_API_KEY=""']:
            with self.subTest(text=repr(text)):
                self.write(text)
                with self.assertRaises(credentials.CredentialError) as caught:
                    credentials.get_key('OPENAI_API_KEY')
                self.assertEqual(str(caught.exception), 'Local credential file is unavailable or invalid')
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(credentials.status('OPENAI_API_KEY'), {'present': False, 'source': 'unavailable'})
        self.vault.assert_not_called()

    def test_invalid_encoding_and_oversized_files_fail_closed(self):
        for data in [b'OPENAI_API_KEY=\xff', b'#' * (credentials.MAX_ENV_BYTES + 1)]:
            self.file.write_bytes(data)
            with self.assertRaises(credentials.CredentialError):
                credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_exact_size_limit_is_accepted(self):
        self.file.write_bytes(b'#' * credentials.MAX_ENV_BYTES)
        self.assertIsNone(credentials.get_key('OPENAI_API_KEY'))

    def test_directory_instead_of_file_is_rejected(self):
        self.file.mkdir()
        with self.assertRaises(credentials.CredentialError):
            credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_hardlinked_file_is_rejected(self):
        original = self.file.with_name('synthetic-source')
        original.write_text('OPENAI_API_KEY=synthetic-value', encoding='utf-8')
        try:
            os.link(original, self.file)
        except OSError:
            self.skipTest('Filesystem cannot create test hard links')
        with self.assertRaises(credentials.CredentialError):
            credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_symlink_file_is_rejected(self):
        original = self.file.with_name('synthetic-source')
        original.write_text('OPENAI_API_KEY=synthetic-value', encoding='utf-8')
        try:
            self.file.symlink_to(original)
        except OSError:
            self.skipTest('Symlink creation requires additional privileges')
        with self.assertRaises(credentials.CredentialError):
            credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_symlink_ancestor_is_rejected(self):
        original = self.file.parent / 'synthetic-directory'
        original.mkdir()
        link = self.file.parent / 'linked-directory'
        try:
            link.symlink_to(original, target_is_directory=True)
        except OSError:
            self.skipTest('Symlink creation requires additional privileges')
        with patch.object(credentials, 'LOCAL_ENV_FILE', link / '.env.local'):
            with self.assertRaises(credentials.CredentialError):
                credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_reparse_attribute_is_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        with self.assertRaises(credentials.CredentialError):
            credentials._reject_link(info)

    def test_changed_identity_between_inspection_and_open_is_rejected(self):
        self.write('OPENAI_API_KEY=synthetic-value')
        info = self.file.stat()
        opened = SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0,
                                 st_nlink=1, st_size=info.st_size,
                                 st_dev=info.st_dev, st_ino=info.st_ino + 1)
        with patch.object(credentials.os, 'fstat', return_value=opened):
            with self.assertRaises(credentials.CredentialError):
                credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_invalid_environment_does_not_fall_back(self):
        os.environ['OPENAI_API_KEY'] = 'synthetic secret'
        self.write('OPENAI_API_KEY=synthetic-valid')
        with self.assertRaises(credentials.CredentialError):
            credentials.get_key('OPENAI_API_KEY')
        self.vault.assert_not_called()

    def test_names_and_values_are_restricted(self):
        for name in ['UNSUPPORTED', '', None, []]:
            with self.assertRaises(credentials.CredentialError):
                credentials.get_key(name)
        for value in ['', None, 123, 'synthetic secret', 'synthetic\tsecret',
                      'synthetic\nsecret', 'synthetic\x00secret', 'synthetic\x7fsecret',
                      'synthetic-\u00e9', 'synthetic-\ud800', 'x' * 2049]:
            with self.assertRaises(credentials.CredentialError) as caught:
                credentials.validate_key(value)
            self.assertNotIn('synthetic', str(caught.exception))
        credentials.validate_key('x' * 2048)
        self.vault.assert_not_called()

    def test_vault_failure_is_only_reported_as_unavailable(self):
        self.vault.side_effect = credentials.CredentialError('Synthetic diagnostic')
        self.assertEqual(credentials.status('OPENAI_API_KEY'), {'present': False, 'source': 'unavailable'})

    def test_store_preserves_existing_value_without_explicit_replace(self):
        self.vault.return_value = 'synthetic-existing'
        with self.assertRaises(credentials.CredentialError):
            credentials.store_key('OPENAI_API_KEY', 'synthetic-new')
        self.native.assert_not_called()

    def test_mocked_store_clears_native_buffer_after_write(self):
        api = MagicMock()

        def write(pointer, flags):
            item = ctypes.cast(pointer, ctypes.POINTER(credentials._Credential)).contents
            self.assertEqual(ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize), b'synthetic-value')
            return True

        api.CredWriteW.side_effect = write
        self.native.side_effect = None
        self.native.return_value = api
        with patch.object(credentials.ctypes, 'memset', wraps=ctypes.memset) as clear:
            credentials.store_key('OPENAI_API_KEY', 'synthetic-value')
        api.CredWriteW.assert_called_once()
        clear.assert_called_once()
        self.assertEqual(clear.call_args.args[1:], (0, len('synthetic-value')))


@unittest.skipUnless(os.name == 'nt', 'Windows credential integration only')
class NativeCredentialTests(unittest.TestCase):
    def test_unique_temporary_vault_roundtrip_and_replacement(self):
        # Never use or enumerate production targets, even on a configured machine.
        target = 'DuetOrchestrator/tests/' + uuid.uuid4().hex
        targets = {'OPENAI_API_KEY': target,
                   'ANTHROPIC_API_KEY': target + '-unused'}
        try:
            api = credentials._native()
        except OSError:
            self.skipTest('Windows credential API unavailable in this environment')
        api.CredDeleteW.argtypes = [credentials.wintypes.LPCWSTR,
                                   credentials.wintypes.DWORD, credentials.wintypes.DWORD]
        api.CredDeleteW.restype = credentials.wintypes.BOOL

        def cleanup():
            if not api.CredDeleteW(target, 1, 0) and ctypes.get_last_error() != 1168:
                self.fail('Could not remove the temporary synthetic test credential')

        with patch.object(credentials, 'TARGETS', targets):
            try:
                self.assertIsNone(credentials.read_stored('OPENAI_API_KEY'))
                credentials.store_key('OPENAI_API_KEY', 'synthetic-native-one')
            except credentials.CredentialError:
                self.skipTest('Windows credential access unavailable in this environment')
            self.addCleanup(cleanup)
            self.assertEqual(credentials.read_stored('OPENAI_API_KEY'), 'synthetic-native-one')
            with self.assertRaises(credentials.CredentialError):
                credentials.store_key('OPENAI_API_KEY', 'synthetic-native-two')
            self.assertEqual(credentials.read_stored('OPENAI_API_KEY'), 'synthetic-native-one')
            credentials.store_key('OPENAI_API_KEY', 'synthetic-native-two', replace=True)
            self.assertEqual(credentials.read_stored('OPENAI_API_KEY'), 'synthetic-native-two')


if __name__ == '__main__':
    unittest.main()
