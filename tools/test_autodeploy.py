import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'deploy/autodeploy.py'
spec = importlib.util.spec_from_file_location('pocket_autodeploy', SOURCE)
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)
SHA = 'a' * 40


def archive(files=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as result:
        for name, raw in (files or {'compose.yaml': b'name: codex-pocket\n', 'Dockerfile': b'FROM example\n'}).items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            item.mode = 0o644
            result.addfile(item, io.BytesIO(raw))
    return buffer.getvalue()


def document():
    return {'name': 'codex-pocket', 'services': {
        name: {'build': {'context': '.'}, 'volumes': [
            {'type': 'volume', 'source': name + '_state', 'target': '/state'}]}
        for name in auto.SERVICES}, 'volumes': {
            name + '_state': {'name': 'existing_' + name} for name in auto.SERVICES}}


class AutoDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.state = self.root / 'repo', self.root / 'state'
        self.repo.mkdir(); self.state.mkdir()
        (self.repo / '.env.docker').write_text('PUBLIC_HOST=example.invalid\n')
        (self.repo / 'local-edit').write_text('keep local changes')
        self.config = dict(repo=str(self.repo), state=str(self.state), branch='master', remote='origin',
                           project='codex-pocket', compose=['docker', 'compose'])
        self.calls = []
        self.fail = None
        self.candidate_document = document()
        self.payload = archive()
        self.sha = SHA

    def fake_git(self, repo, *args, **kwargs):
        self.calls.append(('git', args))
        if args[0] == 'rev-parse': return self.sha.encode()
        if args[0] == 'archive': return self.payload
        return b''

    def fake_compose(self, config, directory, *args, **kwargs):
        self.calls.append(('compose', args, kwargs))
        if args[0] == 'config':
            return json.dumps(self.candidate_document if Path(directory) != self.repo else document()).encode()
        if args[0] == 'ps': return args[-1].encode()
        if args[0] == 'build' and self.fail == 'build': raise RuntimeError('build failed')
        if args[0] == 'up' and 'file' not in kwargs and self.fail in ('start', 'rollback'):
            raise RuntimeError('start failed')
        if args[0] == 'up' and 'file' in kwargs and self.fail == 'rollback':
            raise RuntimeError('rollback failed')
        return b''

    def fake_run(self, args, **kwargs):
        self.calls.append(('run', args))
        if args[:2] == ['docker', 'inspect']:
            return json.dumps([{'Image': 'sha256:old', 'Mounts': [
                {'Type': 'volume', 'Destination': '/state', 'Name': 'existing_' + args[-1]}]}]).encode()
        if args[:2] == ['docker', 'run'] and self.fail == 'tests':
            raise RuntimeError('tests failed')
        return b''

    def execute(self, **kwargs):
        with patch.object(auto, 'git', self.fake_git), patch.object(auto, 'compose', self.fake_compose), \
             patch.object(auto, 'run_command', self.fake_run), patch.object(auto, 'health') as health:
            auto.deploy(self.config, **kwargs)
            return health

    def read_state(self):
        return json.loads((self.state / 'state.json').read_text())

    def test_success_exact_master_snapshot_preserves_worktree_and_marks_after_health(self):
        health = self.execute()
        state = self.read_state()
        self.assertEqual(state['deployed_commit'], SHA)
        self.assertNotIn('pending', state)
        self.assertEqual((self.repo / 'local-edit').read_text(), 'keep local changes')
        self.assertTrue((Path(state['release']) / 'Dockerfile').exists())
        health.assert_called_once()
        fetch = next(c[1] for c in self.calls if c[0] == 'git' and c[1][0] == 'fetch')
        self.assertIn('+refs/heads/master:refs/pocket-autodeploy/candidate', fetch)
        tests = next(c[1] for c in self.calls if c[0] == 'run' and c[1][:2] == ['docker', 'run'])
        self.assertIn('POCKET_CA_FILE', tests)
        self.assertIn('--read-only', tests)
        self.assertIn('none', tests)
        self.assertNotIn('--volume', tests)

    def test_unchanged_commit_skips_build_and_restart(self):
        self.execute(); self.calls.clear(); self.execute()
        self.assertFalse(any(c[0] == 'compose' for c in self.calls))

    def test_main_branch_can_be_selected(self):
        self.config['branch'] = 'main'
        self.execute()
        fetch = next(c[1] for c in self.calls if c[0] == 'git' and c[1][0] == 'fetch')
        self.assertIn('+refs/heads/main:refs/pocket-autodeploy/candidate', fetch)

    def test_build_or_test_failure_keeps_running_service_and_can_retry(self):
        for failure in ('build', 'tests'):
            with self.subTest(failure=failure):
                self.fail = failure
                with self.assertRaises(RuntimeError): self.execute(retry=True)
                self.assertNotIn('deployed_commit', self.read_state())
                self.assertFalse(any(c[0] == 'compose' and c[1][0] == 'up' for c in self.calls))
        self.fail = None
        self.execute(retry=True)
        self.assertEqual(self.read_state()['deployed_commit'], SHA)

    def test_failed_revision_cools_down_but_new_commit_runs(self):
        self.fail = 'build'
        with self.assertRaises(RuntimeError): self.execute()
        self.calls.clear(); self.execute()
        self.assertFalse(any(c[0] == 'compose' for c in self.calls))
        self.fail = None; self.sha = 'b' * 40
        self.execute()
        self.assertEqual(self.read_state()['deployed_commit'], 'b' * 40)

    def test_start_failure_restores_pinned_previous_image_manifest(self):
        self.fail = 'start'
        self.execute_failure()
        state = self.read_state()
        self.assertNotIn('deployed_commit', state)
        self.assertNotIn('pending', state)
        restoration = next(c for c in self.calls if c[0] == 'compose' and c[1][0] == 'up' and c[2].get('file'))
        saved = json.loads(Path(restoration[2]['file']).read_text())
        self.assertIn(':rollback-', saved['services']['gateway']['image'])
        self.assertNotIn('build', saved['services']['gateway'])

    def execute_failure(self):
        with self.assertRaises(RuntimeError): self.execute()

    def test_rollback_failure_is_recorded_for_operator_attention(self):
        self.fail = 'rollback'; self.execute_failure()
        self.assertTrue(self.read_state()['rollback_failed'])
        self.assertIn('pending', self.read_state())

    def test_health_failure_rolls_back_and_does_not_mark_success(self):
        with patch.object(auto, 'git', self.fake_git), patch.object(auto, 'compose', self.fake_compose), \
             patch.object(auto, 'run_command', self.fake_run), patch.object(auto, 'health', side_effect=[RuntimeError('unhealthy'), None]):
            with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
                auto.deploy(self.config)
        self.assertNotIn('deployed_commit', self.read_state())
        self.assertNotIn('pending', self.read_state())

    def test_success_is_not_recorded_until_health_check_passes(self):
        def check(*args, **kwargs):
            state = self.read_state()
            self.assertIn('pending', state)
            self.assertNotIn('deployed_commit', state)
        with patch.object(auto, 'git', self.fake_git), patch.object(auto, 'compose', self.fake_compose), \
             patch.object(auto, 'run_command', self.fake_run), patch.object(auto, 'health', side_effect=check):
            auto.deploy(self.config)
        self.assertEqual(self.read_state()['deployed_commit'], SHA)

    @unittest.skipIf(os.name == 'nt', 'Linux file locks are verified in CI')
    def test_concurrent_process_skips_when_lock_is_held(self):
        import fcntl
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(self.config))
        with (self.state / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sys, 'argv', ['autodeploy.py', 'run', '--config', str(config_path)]), patch.object(auto, 'deploy') as deploy:
                auto.main()
            deploy.assert_not_called()

    def test_volume_change_rejected_before_build(self):
        self.candidate_document['volumes']['gateway_state']['name'] = 'new_empty_volume'
        with self.assertRaisesRegex(ValueError, 'Persistent volume'): self.execute()
        self.assertFalse(any(c[0] == 'compose' and c[1][0] in ('build', 'up') for c in self.calls))

    def test_interrupted_deployment_rolls_back_before_fetching(self):
        rollback = self.state / 'previous.json'
        rollback.write_text('{}')
        auto.save_json(self.state / 'state.json', {'pending': {'active': str(self.repo), 'rollback': str(rollback)}})
        self.execute()
        self.assertEqual(self.calls[0][0:2], ('compose', ('up', '-d', '--no-build', '--force-recreate', *auto.SERVICES)))
        self.assertNotIn('pending', self.read_state())

    def test_archive_traversal_and_links_are_rejected(self):
        with self.assertRaises(ValueError): auto.unpack_snapshot(archive({'../outside': b'bad'}), self.root / 'snapshot')
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w') as output:
            item = tarfile.TarInfo('link'); item.type = tarfile.SYMTYPE; item.linkname = '/etc'
            output.addfile(item)
        with self.assertRaises(ValueError): auto.unpack_snapshot(raw.getvalue(), self.root / 'snapshot')

    @unittest.skipUnless(shutil.which('git'), 'git required')
    def test_real_git_fetch_master_with_dirty_main_checkout(self):
        def command(*args, cwd):
            try:
                return subprocess.check_output(['git', *args], cwd=cwd, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as error:
                raise AssertionError(error.output.decode(errors='replace')) from error
        upstream = self.root / 'upstream'; upstream.mkdir()
        command('init', '-b', 'master', cwd=upstream)
        command('config', 'user.name', 'Test', cwd=upstream)
        command('config', 'user.email', 'test@example.invalid', cwd=upstream)
        for name, content in {'compose.yaml': 'name: codex-pocket\n', 'Dockerfile': 'FROM test\n', 'code.txt': 'master code'}.items():
            (upstream / name).write_text(content)
        command('add', '.', cwd=upstream); command('commit', '-m', 'initial', cwd=upstream)
        bundle = self.root / 'master.bundle'
        command('bundle', 'create', str(bundle), '--all', cwd=upstream)
        checkout = self.root / 'checkout'
        command('clone', str(bundle), str(checkout), cwd=self.root)
        command('checkout', '-b', 'main', cwd=checkout)
        (checkout / 'code.txt').write_text('uncommitted edit')
        (checkout / '.env.docker').write_text('PUBLIC_HOST=example.invalid\n')
        self.repo = checkout; self.config['repo'] = str(checkout)
        with patch.object(auto, 'compose', self.fake_compose), patch.object(auto, 'run_command') as runner, patch.object(auto, 'health'):
            def dispatch(args, **kwargs):
                if args[0] == 'git':
                    return subprocess.check_output(args, stderr=subprocess.STDOUT)
                return self.fake_run(args, **kwargs)
            runner.side_effect = dispatch
            auto.deploy(self.config)
        self.assertEqual((checkout / 'code.txt').read_text(), 'uncommitted edit')
        self.assertEqual(command('branch', '--show-current', cwd=checkout).strip(), b'main')
        self.assertEqual((Path(self.read_state()['release']) / 'code.txt').read_text(), 'master code')


if __name__ == '__main__':
    unittest.main()
