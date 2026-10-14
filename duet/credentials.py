"""Resolve only Duet's two API credentials, without printing or copying secrets."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat

TARGETS = {'OPENAI_API_KEY': 'DuetOrchestrator/OpenAI',
           'ANTHROPIC_API_KEY': 'DuetOrchestrator/Anthropic'}
LOCAL_ENV_FILE = Path(__file__).resolve().parents[1] / '.env.local'
MAX_ENV_BYTES = 16 * 1024


class CredentialError(RuntimeError):
    pass


class _Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def _native():
    if os.name != 'nt':
        raise CredentialError('Windows Credential Manager is unavailable on this platform')
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _target(name):
    if not isinstance(name, str) or name not in TARGETS:
        raise CredentialError('Unsupported credential name')
    return TARGETS[name]


def validate_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(not 33 <= ord(c) <= 126 for c in value):
        raise CredentialError('Credential must contain only printable ASCII without spaces')


def _reject_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise CredentialError('Local credential file is unavailable or invalid')


def _check_file(info):
    _reject_link(info)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ENV_BYTES:
        raise CredentialError('Local credential file is unavailable or invalid')


def _local_values():
    """Read the one configured file; never search parents or evaluate its contents."""
    try:
        path = Path(LOCAL_ENV_FILE)
        if not path.is_absolute():
            raise CredentialError('Local credential file is unavailable or invalid')
        # Check each ancestor without resolving links, before opening the file.
        for parent in reversed(path.parents):
            info = parent.lstat()
            _reject_link(info)
            if not stat.S_ISDIR(info.st_mode):
                raise CredentialError('Local credential file is unavailable or invalid')
        try:
            before = path.lstat()
        except FileNotFoundError:
            return {}
        _check_file(before)
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            _check_file(opened)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise CredentialError('Local credential file is unavailable or invalid')
            content = stream.read(MAX_ENV_BYTES + 1)
            after = path.lstat()
            _check_file(after)
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino) or len(content) > MAX_ENV_BYTES:
                raise CredentialError('Local credential file is unavailable or invalid')
        values = {}
        for line in content.decode('utf-8-sig').split('\n'):
            line = line.strip(' \t\r')
            if not line or line.startswith('#'):
                continue
            name, separator, value = line.partition('=')
            name, value = name.strip(' \t'), value.strip(' \t')
            if separator != '=' or name not in TARGETS or name in values:
                raise CredentialError('Local credential file is unavailable or invalid')
            if value[:1] in {'"', "'"}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise CredentialError('Local credential file is unavailable or invalid')
                value = value[1:-1]
            elif '"' in value or "'" in value:
                raise CredentialError('Local credential file is unavailable or invalid')
            validate_key(value)
            values[name] = value
        return values
    except (OSError, UnicodeError, ValueError, CredentialError):
        raise CredentialError('Local credential file is unavailable or invalid') from None


def read_stored(name):
    target = _target(name)
    if os.name != 'nt':
        return None
    api = _native()
    pointer = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        raise CredentialError('Windows Credential Manager could not be accessed')
    try:
        item = pointer.contents
        if not 0 < item.CredentialBlobSize <= 2048 or not item.CredentialBlob:
            raise CredentialError('Stored credential is invalid')
        try:
            value = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode('utf-8')
        except UnicodeError:
            raise CredentialError('Stored credential is invalid') from None
        validate_key(value)
        return value
    finally:
        api.CredFree(pointer)


def store_key(name, value, *, replace=False):
    target = _target(name)
    validate_key(value)
    if not replace and read_stored(name) is not None:
        raise CredentialError('Credential already exists; replacement must be explicit')
    api = _native()
    encoded = value.encode('utf-8')
    buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    item = _Credential()
    item.Type = 1  # CRED_TYPE_GENERIC
    item.TargetName = target
    item.Comment = 'Duet Orchestrator API key; current Windows user only'
    item.CredentialBlobSize = len(encoded)
    item.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    item.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: same user, same computer
    item.UserName = 'duet'
    try:
        if not api.CredWriteW(ctypes.byref(item), 0):
            raise CredentialError('Windows Credential Manager could not save the credential')
    finally:
        ctypes.memset(buffer, 0, len(encoded))


def _resolve(name):
    _target(name)
    value = os.environ.get(name)
    if value:
        validate_key(value)
        return value, 'environment'
    value = _local_values().get(name)
    if value is not None:
        return value, '.env.local'
    value = read_stored(name)
    return value, 'windows-credential-manager' if value else 'missing'


def get_key(name):
    return _resolve(name)[0]


def status(name):
    try:
        value, source = _resolve(name)
        return {'present': bool(value), 'source': source}
    except CredentialError:
        return {'present': False, 'source': 'unavailable'}
