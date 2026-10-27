"""Container lifecycle and explicit operator commands. Never logs account or device keys."""
import http.client
import json
import os
from pathlib import Path
import runpy
import signal
import socket
import ssl
import subprocess
import sys
import time


def initialize():
    os.umask(0o077)
    home = Path(os.environ['CODEX_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    config = home / 'config.toml'
    if not config.exists():
        config.write_text('cli_auth_credentials_store = "file"\n', encoding='utf-8')
    workspace = Path(os.environ['POCKET_WORKSPACE'])
    workspace.mkdir(parents=True, exist_ok=True)
    instructions = workspace / 'AGENTS.md'
    if not instructions.exists():
        instructions.write_text(Path('/app/default-AGENTS.md').read_text('utf-8'), encoding='utf-8')


def tls_health():
    context = ssl.create_default_context(cafile=os.environ['POCKET_CA_FILE'])
    host = os.environ['POCKET_PUBLIC_HOST']
    with socket.create_connection(('127.0.0.1', 8443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secured:
            secured.sendall(f'GET /healthz HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode('ascii'))
            response = http.client.HTTPResponse(secured)
            response.begin()
            if response.status != 200:
                raise RuntimeError(f'TLS health check returned {response.status}')
            print(response.read().decode('utf-8'))


def serve():
    initialize()
    model = Path(os.environ['POCKET_MODEL_DIR'])
    if not all((model / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json')):
        raise RuntimeError('Speech model is not ready. Run bash deploy/docker.sh up to prepare it.')
    children = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Only this relay is published on HOST LOOPBACK. The bridge retains all token/origin checks.
        children.append(subprocess.Popen(['socat', 'TCP-LISTEN:8789,bind=0.0.0.0,reuseaddr,fork', 'TCP:127.0.0.1:8787']))
        children.append(subprocess.Popen([sys.executable, '/app/bridge/bridge.py', '--host', '127.0.0.1', '--http-port', '8787'], cwd=os.environ['POCKET_WORKSPACE']))
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.3)
        return 0 if stopping else 1
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main(command):
    if command == 'local-health':
        from urllib.request import urlopen
        with urlopen('http://127.0.0.1:8787/healthz', timeout=3) as response:
            return 0 if response.status == 200 else 1
    if command == 'health':
        tls_health()
        return 0
    if command == 'export':
        package = runpy.run_path('/app/deploy/export_connection.py')['connection']()
        print(json.dumps(package, ensure_ascii=False, indent=2))
        return 0
    if command == 'console':
        url = (Path(os.environ['POCKET_STATE_DIR']) / 'console-url.txt').read_text('utf-8')
        print(url.replace(':8787/', ':' + os.environ.get('POCKET_CONSOLE_PORT', '8787') + '/'))
        return 0
    if command == 'serve':
        return serve()
    initialize()
    if command in ('login', 'login-status'):
        args = ['--device-auth'] if command == 'login' else ['status']
        os.execvp('codex', ['codex', 'login', *args])
    if command == 'model':
        model = Path(os.environ['POCKET_MODEL_DIR'])
        if all((model / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json')):
            print('Reusing complete speech model:', model)
            return 0
        return subprocess.call([sys.executable, '/app/setup-speech.py', '--model', os.environ['POCKET_SPEECH_MODEL'], '--output', str(model)])
    raise ValueError('Unknown container command: ' + command)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'serve'))
