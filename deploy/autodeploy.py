#!/usr/bin/env python3
"""Pull an exact main snapshot and update the existing Docker project.

Linux/systemd operator tool. Does not check out/reset the user's repository,
install models, change credentials, or delete persistent volumes.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

CONFIG = Path('/etc/codex-pocket-autodeploy.json')
SCRIPT = Path('/usr/local/lib/codex-pocket-autodeploy/autodeploy.py')
SERVICES = ('gateway', 'ingress')


def save_json(path, data):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as output:
        json.dump(data, output, indent=2)
    temporary.replace(path)


def run_command(args, *, capture=False, timeout=1800):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_SSH_COMMAND='ssh -o BatchMode=yes')
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE if capture else None,
                            check=True, timeout=timeout)
    return result.stdout if capture else b''


def git(repo, *args, capture=True):
    return run_command(['git', '-c', f'safe.directory={repo}', '-C', str(repo), *args], capture=capture, timeout=120)


def compose(config, directory, *args, capture=False, file=None, timeout=1800):
    directory = Path(directory)
    selected = file or (directory / '.autodeploy-compose.json' if (directory / '.autodeploy-compose.json').exists() else directory / 'compose.yaml')
    return run_command([*config['compose'], '-p', config['project'], '--project-directory', str(directory),
                        '--env-file', str(directory / '.env.docker'), '-f', str(selected),
                        *args], capture=capture, timeout=timeout)


def unpack_snapshot(data, destination):
    """No links, traversal, device files or repository credentials in a snapshot."""
    destination = Path(destination).resolve()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = destination / member.name
            if not path.resolve().is_relative_to(destination) or not (member.isfile() or member.isdir()):
                raise ValueError('Unsupported archive entry: ' + member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.extractfile(member).read())
                path.chmod(0o755 if member.mode & 0o111 else 0o644)


def volume_map(document):
    result = {}
    for service in SERVICES:
        volumes = document['services'][service].get('volumes', [])
        result[service] = {item['target']: document['volumes'][item['source']]['name']
                           for item in volumes if item['type'] == 'volume'}
    return result


def verify_volumes(candidate, running):
    if set(candidate['services']) != set(SERVICES):
        raise ValueError('Expected the existing gateway/ingress topology; review deployment-tool changes first')
    if volume_map(candidate) != running:
        raise ValueError('Persistent volume names or mount targets changed; refusing to start with different data')


def health(config, directory, *, file=None, attempts=40):
    for attempt in range(attempts):
        try:
            compose(config, directory, 'exec', '--user', '10001:10001', '-T', 'gateway',
                    'python', '/app/docker/runtime.py', 'health', file=file, timeout=10)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt + 1 < attempts:
                time.sleep(3)
    raise RuntimeError('HTTPS gateway health check did not pass')


def capture_running(config, directory, transaction):
    document = json.loads(compose(config, directory, 'config', '--format', 'json', capture=True))
    mounts = {}
    for service in SERVICES:
        container = compose(config, directory, 'ps', '-q', service, capture=True).decode().strip()
        if not container or '\n' in container:
            raise RuntimeError('Expected one running container for ' + service)
        inspect = json.loads(run_command(['docker', 'inspect', container], capture=True))[0]
        mounts[service] = {item['Destination']: item['Name'] for item in inspect['Mounts'] if item['Type'] == 'volume'}
        tag = f"{config['project']}-autodeploy-{service}:rollback-{transaction.name}"
        run_command(['docker', 'image', 'tag', inspect['Image'], tag])
        document['services'][service].pop('build', None)
        document['services'][service]['image'] = tag
    rollback = transaction / 'rollback.json'
    save_json(rollback, document)
    return rollback, mounts


def deploy(config, *, retry=False):
    repo, state_dir = Path(config['repo']), Path(config['state'])
    state_path = state_dir / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if state.get('pending'):
        pending = state['pending']
        print('Recovering an interrupted deployment before checking main.', flush=True)
        compose(config, pending['active'], 'up', '-d', '--no-build', '--force-recreate', *SERVICES, file=pending['rollback'])
        health(config, pending['active'], file=pending['rollback'])
        state.pop('pending')
        save_json(state_path, state)
    # Fetch only the configured branch. No interactive Git/SSH credential prompt.
    ref = 'refs/pocket-autodeploy/candidate'
    git(repo, 'fetch', '--no-tags', config['remote'], f'+refs/heads/{config["branch"]}:{ref}', capture=False)
    sha = git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    if not re.fullmatch('[0-9a-f]{40,64}', sha):
        raise ValueError('Invalid Git commit ID')
    environment = (repo / '.env.docker').read_bytes()
    key = hashlib.sha256(sha.encode() + b'\0' + environment).hexdigest()
    if state.get('deployed_key') == key:
        print('No new main commit or environment change.', flush=True)
        return
    if not retry and state.get('failed_key') == key and time.time()-state.get('failed_at', 0) < 900:
        print('Failed revision is cooling down; retry after 15 minutes or use run --retry.', flush=True)
        return
    transaction = state_dir / ('release-' + str(time.time_ns()))
    transaction.mkdir(mode=0o700)
    active = Path(state.get('release', config['repo']))
    rollback, running_mounts = capture_running(config, active, transaction)
    source = transaction / 'source'
    source.mkdir()
    changed_runtime = False
    try:
        unpack_snapshot(git(repo, 'archive', '--format=tar', sha), source)
        (source / '.env.docker').write_bytes(environment)
        (source / '.env.docker').chmod(0o600)
        candidate = json.loads(compose(config, source, 'config', '--format', 'json', capture=True, file=source / 'compose.yaml'))
        verify_volumes(candidate, running_mounts)
        for service in SERVICES:
            candidate['services'][service]['image'] = f"{config['project']}-autodeploy-{service}:{sha[:12]}-{key[:12]}"
        save_json(source / '.autodeploy-compose.json', candidate)
        print('Building main commit ' + sha, flush=True)
        compose(config, source, 'build', *SERVICES)
        # No production volumes or network are available to the candidate's tests.
        run_command(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                     '--tmpfs', '/tmp:rw,size=256m', '--entrypoint', 'env',
                     candidate['services']['gateway']['image'], '-u', 'POCKET_STATE_DIR',
                     '-u', 'POCKET_WORKSPACE', '-u', 'POCKET_EXTERNAL_SANDBOX', 'python', '-m',
                     'unittest', 'discover', '-s', '/app/bridge', '-p', 'test_*.py', '-v'])
        state['pending'] = dict(active=str(active), rollback=str(rollback))
        save_json(state_path, state)
        changed_runtime = True
        compose(config, source, 'up', '-d', '--no-build', '--force-recreate', *SERVICES)
        health(config, source)
    except Exception:
        if changed_runtime:
            print('Deployment failed; restoring previous images and Compose configuration.', flush=True)
            try:
                compose(config, active, 'up', '-d', '--no-build', '--force-recreate', *SERVICES, file=rollback)
                health(config, active, file=rollback)
                state.pop('pending', None)
            except Exception:
                print('ROLLBACK FAILED. Stop the timer and inspect the service log.', file=sys.stderr, flush=True)
                state['rollback_failed'] = True
        state.update(failed_key=key, failed_at=time.time(), failed_commit=sha)
        save_json(state_path, state)
        raise
    state.update(deployed_key=key, deployed_commit=sha, release=str(source), deployed_at=time.time())
    for field in ('failed_key', 'failed_at', 'failed_commit', 'pending'):
        state.pop(field, None)
    save_json(state_path, state)
    print('Deployed and verified main commit ' + sha, flush=True)


def install(args):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise RuntimeError('Install on the Linux server using sudo python3')
    repo = Path(args.repo).resolve()
    for name in ('compose.yaml', '.env.docker', 'Dockerfile'):
        if not (repo / name).is_file():
            raise ValueError('Not the existing Docker deployment directory: missing ' + name)
    git(repo, 'rev-parse', '--git-dir')
    git(repo, 'check-ref-format', 'refs/heads/' + args.branch)
    if not re.fullmatch('[A-Za-z0-9_.-]+', args.remote) or args.remote.startswith('-'):
        raise ValueError('Invalid Git remote name')
    git(repo, 'remote', 'get-url', args.remote)
    git(repo, 'ls-remote', '--exit-code', '--heads', args.remote, 'refs/heads/' + args.branch)
    engine = ['docker', 'compose']
    try:
        run_command([*engine, 'version'], capture=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        version = run_command(['docker-compose', 'version', '--short'], capture=True).decode().lstrip('v')
        if not version.startswith('2.'):
            raise RuntimeError('Docker Compose v2 is required')
        engine = ['docker-compose']
    resolved = json.loads(run_command([*engine, '--project-directory', str(repo), '--env-file', str(repo / '.env.docker'),
                                      '-f', str(repo / 'compose.yaml'), 'config', '--format', 'json'], capture=True))
    project = resolved['name']
    if not re.fullmatch('[a-z0-9][a-z0-9_-]*', project):
        raise ValueError('Invalid Docker project name')
    config = dict(repo=str(repo), remote=args.remote, branch=args.branch, project=project,
                  compose=engine, state='/var/lib/codex-pocket-autodeploy')
    for service in SERVICES:
        if not compose(config, repo, 'ps', '-q', service, capture=True).strip():
            raise RuntimeError('Deploy and start the existing ' + service + ' first')
    Path(config['state']).mkdir(mode=0o700, parents=True, exist_ok=True)
    SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != SCRIPT:
        shutil.copyfile(__file__, SCRIPT)
    SCRIPT.chmod(0o755)
    save_json(CONFIG, config)
    Path('/etc/systemd/system/codex-pocket-autodeploy.service').write_text('''[Unit]
Description=Deploy Codex Pocket main updates
Wants=network-online.target
After=network-online.target docker.service
[Service]
Type=oneshot
UMask=0077
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /usr/local/lib/codex-pocket-autodeploy/autodeploy.py run
TimeoutStartSec=infinity
''')
    Path('/etc/systemd/system/codex-pocket-autodeploy.timer').write_text('''[Unit]
Description=Check Codex Pocket main every minute
[Timer]
OnBootSec=30s
OnUnitInactiveSec=60s
AccuracySec=5s
[Install]
WantedBy=timers.target
''')
    run_command(['systemctl', 'daemon-reload'])
    run_command(['systemctl', 'enable', '--now', 'codex-pocket-autodeploy.timer'])
    run_command(['systemctl', 'start', '--no-block', 'codex-pocket-autodeploy.service'])
    print('Installed. First deployment started. Watch: journalctl -u codex-pocket-autodeploy.service -f')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('install', 'run', 'status'))
    parser.add_argument('--repo', default='.')
    parser.add_argument('--branch', default='main')
    parser.add_argument('--remote', default='origin')
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--retry', action='store_true')
    args = parser.parse_args()
    if args.command == 'install':
        install(args)
        return
    config = json.loads(args.config.read_text())
    if args.command == 'status':
        state = Path(config['state']) / 'state.json'
        print(state.read_text() if state.exists() else 'No deployment recorded yet.')
        return
    import fcntl
    state_dir = Path(config['state'])
    with (state_dir / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another deployment is running; skipped.')
            return
        state_path = state_dir / 'state.json'
        if state_path.exists() and json.loads(state_path.read_text()).get('rollback_failed'):
            raise RuntimeError('Previous rollback failed; resolve it and clear rollback_failed in state.json before retrying')
        deploy(config, retry=args.retry)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Auto-deploy failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
