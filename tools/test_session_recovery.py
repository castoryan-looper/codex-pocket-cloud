import json
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from recover_session import detach_missing_session


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        self.home = self.root / 'codex'; self.home.mkdir()
        self.settings = self.state / 'settings.json'
        self.original = {'thread_id':'abc-id','device_token':'unchanged','console_token':'unchanged','cwd':'/workspace'}
        self.settings.write_text(json.dumps(self.original), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_and_only_remove_matching_reference(self):
        backup = detach_missing_session(self.state, self.home, 'abc-id')
        self.assertEqual(json.loads(backup.read_text()), self.original)
        current = json.loads(self.settings.read_text())
        self.assertNotIn('thread_id', current)
        self.assertEqual({k:v for k,v in self.original.items() if k!='thread_id'}, current)

    def test_changed_id_does_not_modify_or_backup(self):
        with self.assertRaises(ValueError):
            detach_missing_session(self.state, self.home, 'other-id')
        self.assertEqual(json.loads(self.settings.read_text()), self.original)
        self.assertEqual(list(self.state.iterdir()), [self.settings])

    def test_possible_rollout_blocks_reset(self):
        session_dir = self.home / 'archived_sessions'; session_dir.mkdir()
        (session_dir / 'rollout-abc-id.jsonl').write_text('{}')
        with self.assertRaises(ValueError):
            detach_missing_session(self.state, self.home, 'abc-id')
        self.assertEqual(json.loads(self.settings.read_text()), self.original)


if __name__ == '__main__':
    unittest.main()
