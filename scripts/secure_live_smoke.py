"""Run in your own terminal. Hidden key entry, process memory only, no key files."""
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if not sys.stdin.isatty():
        print('Run this script interactively in your own terminal; never paste keys into chat.')
        return 2
    print('Real OpenAI + Claude smoke test. Maximum ledger budget: US$1. Keys are not saved.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            for key in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY'):
                if not os.environ.get(key):
                    os.environ[key] = getpass.getpass(key + ' (hidden): ').strip()
        from duet.__main__ import main as cli
        directory = ROOT.parent / 'live-validation'
        if directory.exists():
            print('Output already exists. Inspect it before repeating; budget ledger is shared.')
            return 2
        return cli(['live-smoke', '--directory', str(directory),
                    '--config', str(ROOT / 'examples/live-smoke.json'),
                    '--state-dir', str(ROOT.parent / '.duet-live-smoke-state')])
    finally:
        os.environ.pop('OPENAI_API_KEY', None)
        os.environ.pop('ANTHROPIC_API_KEY', None)


if __name__ == '__main__':
    raise SystemExit(main())
