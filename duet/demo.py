"""Deterministic peer collaboration fixture. It makes no external API calls."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .config import Config
from .engine import Orchestrator
from .providers import Reply
from .sandbox import TestRunner

INITIAL = 'def add(a, b):\n    return a + b\n'
SUBTRACT = INITIAL + '\ndef subtract(a, b):\n    return a - b\n'
FINAL = SUBTRACT + '\ndef multiply(a, b):\n    return a * b\n'
TESTS = '''import unittest
import calculator

class CalculatorTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calculator.add(2, 3), 5)
        self.assertEqual(calculator.add(-3, 2), -1)
    def test_subtract_when_present(self):
        if hasattr(calculator, "subtract"):
            self.assertEqual(calculator.subtract(7, 2), 5)
            self.assertEqual(calculator.subtract(-3, 2), -5)
    def test_multiply_when_present(self):
        if hasattr(calculator, "multiply"):
            self.assertEqual(calculator.multiply(3, 4), 12)
            self.assertEqual(calculator.multiply(-3, 2), -6)
'''


class FixtureProvider:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.delegated = False

    def generate(self, system, prompt, schema, max_output_tokens):
        request = json.loads(prompt)
        self.calls.append(request)
        phase = request['phase']
        if phase == 'plan':
            response = {'tasks': [
                {'agent': 'openai', 'instruction': 'Add subtract(a, b). Delegate implementation to the peer.'},
                {'agent': 'openai', 'instruction': 'Add multiply(a, b), preserving subtract.'}]}
        elif phase == 'review':
            source = request['candidate_files']['calculator.py']
            expected = 'def subtract' if 'subtract(a' in request['instruction'] else 'def multiply'
            response = {'approved': request['tests']['passed'] and expected in source,
                        'feedback': 'Verified functions, preservation of existing behavior and test result.'}
        elif 'subtract(a' in request['instruction'] and self.name == 'openai' and not self.delegated:
            self.delegated = True
            response = {'action': 'delegate', 'message': 'Claude: implement subtract(a, b) preserving add.', 'changes': []}
        else:
            source = SUBTRACT if 'subtract(a' in request['instruction'] else FINAL
            response = {'action': 'changes', 'message': 'Please review this implementation independently.',
                        'changes': [{'path': 'calculator.py', 'content': source}]}
        return Reply(json.dumps(response), 250, 160)


def fixture_config(mode='local-trusted'):
    return Config.from_dict({
        'allowed_paths': ['calculator.py'], 'context_paths': ['calculator.py', 'tests/test_calculator.py'],
        'providers': {name: {'model': 'offline-fixture', 'input_usd_per_million': '1',
                           'output_usd_per_million': '1'} for name in ('openai', 'anthropic')},
        'budget': {'max_usd': '1.00', 'max_calls': 20},
        'tests': {'mode': mode, 'command': ['python', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                  'timeout': 30, 'image': 'python:3.12-slim'},
        'max_tasks': 4, 'max_rounds': 3, 'max_output_tokens': 2048})


def create_fixture(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = root / 'project'
    (repo / 'tests').mkdir(parents=True)
    (repo / 'calculator.py').write_text(INITIAL, encoding='utf-8')
    (repo / 'tests/test_calculator.py').write_text(TESTS, encoding='utf-8')
    (repo / '.gitignore').write_text('__pycache__/\n*.pyc\n', encoding='utf-8')
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=Duet Demo', '-c', 'user.email=duet@localhost', *args], cwd=repo,
            stderr=subprocess.DEVNULL, text=True).strip()
    git('init', '-b', 'main')
    git('add', '.')
    git('commit', '-m', 'Create trusted calculator fixture')
    return repo, git('rev-parse', 'HEAD')


def demo(directory, sandbox='local-trusted'):
    repo, head = create_fixture(directory)
    config = fixture_config(sandbox)
    settings = dict(config.tests)
    if sandbox == 'local-trusted':
        settings['command'] = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
    runner = TestRunner(**settings, allow_local=sandbox == 'local-trusted')
    providers = {name: FixtureProvider(name) for name in ('openai', 'anthropic')}
    result = Orchestrator(repo, Path(directory) / 'state', config, providers, runner=runner).run(
        'Implement subtract and multiply, with both agents implementing and cross-reviewing.')
    final_source = (Path(result['integration_path']) / 'calculator.py').read_text(encoding='utf-8')
    if final_source != FINAL or (repo / 'calculator.py').read_text(encoding='utf-8') != INITIAL:
        raise RuntimeError('Demo source preservation check failed')
    current = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    if current != head:
        raise RuntimeError('Demo modified the original branch')
    result.update(provider_mode='simulated', external_api_calls=0, actual_api_cost_usd='0',
                  original_branch_unchanged=True, final_behavior_verified=True)
    (Path(directory) / 'demo-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
