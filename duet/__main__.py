from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from .config import Config
from .demo import demo
from .engine import Orchestrator
from .providers import OpenAIProvider, AnthropicProvider
from .sandbox import TestRunner


def main(argv=None):
    parser = argparse.ArgumentParser(description='OpenAI + Claude: bounded autonomous peer collaboration')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Report tools and key presence; never print key values')
    fixture = commands.add_parser('demo', help='Run a deterministic end-to-end test with zero API calls')
    fixture.add_argument('--directory', required=True, type=Path, help='New output directory')
    fixture.add_argument('--sandbox', choices=['docker', 'local-trusted'], default='local-trusted')
    smoke = commands.add_parser('live-smoke', help='Real APIs, inert JSON only; no generated code execution')
    smoke.add_argument('--directory', required=True, type=Path)
    smoke.add_argument('--config', type=Path)
    smoke.add_argument('--state-dir', type=Path)
    run = commands.add_parser('run', help='Use real OpenAI and Anthropic APIs')
    run.add_argument('--repo', required=True, type=Path)
    run.add_argument('--config', required=True, type=Path)
    run.add_argument('--task-file', required=True, type=Path)
    run.add_argument('--state-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            from .credentials import status
            openai_status = status('OPENAI_API_KEY')
            anthropic_status = status('ANTHROPIC_API_KEY')
            result = {'python': sys.version.split()[0], 'git_available': bool(shutil.which('git')),
                      'docker_available': bool(shutil.which('docker')),
                      'openai_key_present': openai_status['present'],
                      'anthropic_key_present': anthropic_status['present'],
                      'openai_key_source': openai_status['source'],
                      'anthropic_key_source': anthropic_status['source'],
                      'network_or_credentials_validated': False}
        elif args.command == 'demo':
            result = demo(args.directory, args.sandbox)
        elif args.command == 'live-smoke':
            from .live_smoke import live_smoke
            result = live_smoke(args.directory, args.config, args.state_dir)
        else:
            config = Config.load(args.config)
            if config.tests['mode'] != 'docker':
                raise ValueError('Live runs require Docker isolation; local execution is only for trusted demo fixtures')
            providers = {'openai': OpenAIProvider(config.providers['openai']['model']),
                         'anthropic': AnthropicProvider(config.providers['anthropic']['model'])}
            state = args.state_dir or args.repo.resolve().parent / ('.' + args.repo.name + '-duet-state')
            result = Orchestrator(args.repo, state, config, providers).run(
                args.task_file.read_text(encoding='utf-8-sig'))
            result['provider_mode'] = 'live'
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        # Never print arbitrary exception bodies that could contain model output or secrets.
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__,
                          'guidance': 'Check configuration, doctor, and metadata-only run logs. No ambiguous API call is retried.'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
