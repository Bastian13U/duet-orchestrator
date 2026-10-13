import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from duet.audit import Audit
from duet.budget import BudgetError
from duet.config import Config, ConfigError
from duet.contracts import ContractError, REVIEW, parse
from duet.demo import FixtureProvider, create_fixture, demo, fixture_config
from duet.engine import Orchestrator, RunError
from duet.providers import ProviderError, Reply
from duet.sandbox import TestRunner


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='duet-engine-'))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        def writable_remove(function, path, error):
            os.chmod(path, 0o700)
            function(path)
        shutil.rmtree(self.root, onerror=writable_remove)

    def setup_engine(self, *, config=None, providers=None, runner=None):
        repo, _ = create_fixture(self.root / 'fixture')
        self.providers = providers or {name: FixtureProvider(name) for name in ('openai', 'anthropic')}
        runner = runner or TestRunner(mode='local-trusted', allow_local=True,
            command=[sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
        self.state = self.root / 'state'
        return Orchestrator(repo, self.state, config or fixture_config(), self.providers, runner=runner)

    def test_complete_peer_delegation_two_way_review_git_tests_and_audit(self):
        result = demo(self.root / 'demo')
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['original_branch_unchanged'])
        self.assertTrue(result['final_behavior_verified'])
        self.assertEqual(result['external_api_calls'], 0)
        self.assertEqual(result['budget']['calls'], 6)
        self.assertEqual(len(result['commits']), 2)
        records = [json.loads(s) for s in Path(result['audit']).read_text().splitlines()]
        self.assertEqual({e['sender'] for e in records if e['event'] == 'cross_review'}, {'openai', 'anthropic'})
        previous = '0' * 64
        for record in records:
            digest = record.pop('hash')
            self.assertEqual(record['previous'], previous)
            self.assertEqual(hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(), digest)
            previous = digest
        self.assertNotIn('def subtract', Path(result['audit']).read_text())

    def test_reviewer_rejection_exhausts_rounds_without_integration(self):
        class Reject(FixtureProvider):
            def generate(self, system, prompt, schema, max_output_tokens):
                if json.loads(prompt)['phase'] == 'review':
                    return Reply('{"approved":false,"feedback":"Fix edge cases"}', 5, 5)
                return super().generate(system, prompt, schema, max_output_tokens)
        engine = self.setup_engine(providers={name: Reject(name) for name in ('openai', 'anthropic')})
        with self.assertRaises(RunError):
            engine.run('Add subtract and multiply')
        events = next(self.state.glob('runs/*/events.jsonl')).read_text()
        self.assertNotIn('task_integrated', events)
        self.assertFalse((self.state / 'active.lock').exists())

    def test_budget_prevents_first_network_call(self):
        config = fixture_config()
        config.budget['max_usd'] = '0.00001'
        engine = self.setup_engine(config=config)
        with self.assertRaises(BudgetError):
            engine.run('Add subtract')
        self.assertFalse(self.providers['openai'].calls)

    def test_network_failure_keeps_durable_reservation(self):
        class Broken(FixtureProvider):
            def generate(self, *args):
                raise ProviderError('Unknown transport outcome')
        engine = self.setup_engine(providers={'openai': Broken('openai'), 'anthropic': FixtureProvider('anthropic')})
        with self.assertRaises(ProviderError):
            engine.run('Add subtract')
        self.assertEqual(engine.budget.snapshot()['calls'], 1)
        self.assertGreater(float(engine.budget.snapshot()['reserved_usd']), 0)
        self.assertEqual(json.loads(next(self.state.glob('runs/*/result.json')).read_text())['status'], 'failed')

    def test_failing_baseline_prevents_provider_calls(self):
        class Fail:
            mode = 'trusted-test-double'
            def run(self, path):
                return {'passed': False, 'returncode': 1, 'output': 'failure'}
        engine = self.setup_engine(runner=Fail())
        with self.assertRaises(RunError):
            engine.run('Anything')
        self.assertFalse(self.providers['openai'].calls)

    def test_active_lock_prevents_calls(self):
        engine = self.setup_engine()
        (self.state / 'active.lock').write_text('existing')
        with self.assertRaises(RunError):
            engine.run('Anything')
        self.assertEqual((self.state / 'active.lock').read_text(), 'existing')


class ContractTests(unittest.TestCase):
    def test_duplicate_fields_and_wrong_boolean_rejected(self):
        for text in ('{"approved":true,"approved":false,"feedback":"x"}',
                     '{"approved":1,"feedback":"x"}', '{"approved":true,"feedback":"x","command":"bad"}'):
            with self.assertRaises(ContractError):
                parse(text, REVIEW)

    def test_configuration_rejects_infinite_rates_and_unbounded_tasks(self):
        data = vars(fixture_config()).copy()
        data['max_tasks'] = 999
        with self.assertRaises(ConfigError):
            Config.from_dict(data)
        data['max_tasks'] = 4
        data['providers']['openai']['input_usd_per_million'] = 'NaN'
        with self.assertRaises(ConfigError):
            Config.from_dict(data)

    def test_audit_rejects_prompt_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Audit(Path(tmp) / 'audit.jsonl').emit('request', prompt='secret')
