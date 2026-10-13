"""Real API end-to-end check: models may edit only inert JSON data."""
import json
from pathlib import Path
import subprocess

from .config import Config
from .engine import Orchestrator, RunError
from .providers import OpenAIProvider, AnthropicProvider


def default_smoke_config():
    # Conservative ceilings above documentation checked on 2026-10-13.
    return Config.from_dict({
        'allowed_paths': ['results.json'], 'context_paths': ['results.json'],
        'providers': {
            'openai': {'model': 'gpt-4.1-mini-2025-04-14', 'input_usd_per_million': '0.50', 'output_usd_per_million': '2.00'},
            'anthropic': {'model': 'claude-haiku-4-5-20251001', 'input_usd_per_million': '1.25', 'output_usd_per_million': '6.25'}},
        'budget': {'max_usd': '1.00', 'max_calls': 12},
        'tests': {'mode': 'docker', 'command': ['python', '-m', 'unittest'], 'timeout': 60, 'image': 'python:3.12-slim'},
        'max_tasks': 2, 'max_rounds': 2, 'max_output_tokens': 1536})


class DataValidator:
    """Trusted validator; never imports, evaluates or executes candidate content."""
    mode = 'json-data-only'

    def run(self, path):
        try:
            raw = (Path(path) / 'results.json').read_bytes()
            if len(raw) > 4096:
                raise ValueError('Oversized JSON')
            value = json.loads(raw)
            expected = {'sum': 12, 'product': 35}
            passed = isinstance(value, dict) and set(value) <= set(expected) and all(
                type(item) is int and item == expected[key] for key, item in value.items())
        except (ValueError, OSError, RecursionError):
            passed = False
        return {'passed': passed, 'returncode': 0 if passed else 1,
                'output': 'Validated inert JSON against trusted arithmetic expectations'}


def live_smoke(directory, config_path=None, state_dir=None):
    config = Config.load(config_path) if config_path else default_smoke_config()
    if config.allowed_paths != ['results.json'] or config.context_paths != ['results.json']:
        raise RunError('Live smoke can edit and read only results.json')
    from decimal import Decimal
    if Decimal(config.budget['max_usd']) > Decimal('1.00') or config.budget['max_calls'] > 12:
        raise RunError('Live smoke is limited to US$1 and 12 API calls')
    providers = {'openai': OpenAIProvider(config.providers['openai']['model']),
                 'anthropic': AnthropicProvider(config.providers['anthropic']['model'])}
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = root / 'project'
    repo.mkdir()
    (repo / 'results.json').write_text('{}\n', encoding='utf-8')
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false',
            '-c', 'user.name=Duet Smoke', '-c', 'user.email=duet@localhost', *args], cwd=repo,
            stderr=subprocess.DEVNULL, text=True).strip()
    git('init', '-b', 'main')
    git('add', '.')
    git('commit', '-m', 'Initialize inert JSON smoke fixture')
    initial_head = git('rev-parse', 'HEAD')
    # Shared ledger by default across smoke output directories, so repeats do not reset spend.
    state = Path(state_dir) if state_dir else root.parent / '.duet-live-smoke-state'
    result = Orchestrator(repo, state, config, providers, runner=DataValidator()).run(
        'Create exactly two ordered tasks for results.json. First assign OpenAI to add sum=12 '
        '(5+7). Second assign Anthropic to add product=35 (5*7), preserving sum. '
        'Only JSON data, no code. Each agent must independently review the peer implementation. '
        'Final file must be {"sum":12,"product":35}.')
    value = json.loads((Path(result['integration_path']) / 'results.json').read_text())
    if value != {'sum': 12, 'product': 35} or git('rev-parse', 'HEAD') != initial_head:
        raise RunError('Live smoke final acceptance failed')
    result.update(provider_mode='live', final_behavior_verified=True, original_branch_unchanged=True)
    (root / 'live-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
