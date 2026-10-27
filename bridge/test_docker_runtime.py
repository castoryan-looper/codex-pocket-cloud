import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'docker/runtime.py'


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('pocket_container', SOURCE)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.default = self.root / 'default-AGENTS.md'
        self.default.write_text('Default instructions', encoding='utf-8')
        self.environment = patch.dict(os.environ, {
            'CODEX_HOME': str(self.root / 'state/.codex'),
            'POCKET_WORKSPACE': str(self.root / 'workspace'),
            'POCKET_MODEL_DIR': str(self.root / 'models'),
            'POCKET_SPEECH_MODEL': 'small',
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_initialization_keeps_existing_account_and_workspace(self):
        def mapped_path(value):
            return self.default if value == '/app/default-AGENTS.md' else Path(value)
        original_umask = os.umask(0o077)
        try:
            with patch.object(self.runtime, 'Path', side_effect=mapped_path):
                self.runtime.initialize()
                config = self.root / 'state/.codex/config.toml'
                self.assertIn('cli_auth_credentials_store', config.read_text())
                config.write_text('Existing account configuration', encoding='utf-8')
                instructions = self.root / 'workspace/AGENTS.md'
                instructions.write_text('User project instructions', encoding='utf-8')
                self.runtime.initialize()
                self.assertEqual(config.read_text(), 'Existing account configuration')
                self.assertEqual(instructions.read_text(), 'User project instructions')
        finally:
            os.umask(original_umask)

    def test_partial_model_is_not_treated_as_a_complete_cached_model(self):
        model = self.root / 'models'
        model.mkdir()
        (model / 'model.bin').write_bytes(b'weight fixture')
        with patch.object(self.runtime, 'initialize'), patch.object(self.runtime.subprocess, 'call', return_value=0) as download:
            self.runtime.main('model')
            download.assert_called_once()
        for filename in ('config.json', 'tokenizer.json'):
            (model / filename).write_text('{}')
        with patch.object(self.runtime, 'initialize'), patch.object(self.runtime.subprocess, 'call') as download:
            self.assertEqual(self.runtime.main('model'), 0)
            download.assert_not_called()
