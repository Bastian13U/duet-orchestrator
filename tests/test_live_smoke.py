import json
from pathlib import Path
import tempfile
import unittest

from duet.config import Config
from duet.live_smoke import DataValidator, default_smoke_config


class DataValidatorTests(unittest.TestCase):
    def test_validates_json_without_executing_model_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for content, expected in [('{}', True), ('{"sum":12}', True),
                                      ('{"sum":12,"product":35}', True),
                                      ('{"sum":13}', False), ('{"sum":true}', False),
                                      ('{"code":"__import__(\"os\")"}', False),
                                      ('import os', False), ('[]', False)]:
                (path / 'results.json').write_text(content, encoding='utf-8')
                self.assertEqual(DataValidator().run(path)['passed'], expected)

    def test_live_configuration_is_bounded(self):
        config = Config.load(Path(__file__).resolve().parents[1] / 'examples/live-smoke.json')
        self.assertEqual(config, default_smoke_config())
        self.assertEqual(config.allowed_paths, ['results.json'])
        self.assertEqual(config.budget['max_usd'], '1.00')
        self.assertLessEqual(config.budget['max_calls'], 12)
