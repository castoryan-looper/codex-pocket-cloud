"""Loopback-only USB configuration UI. No AI actions or audio bridge."""
import argparse
import hmac
import json
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]


class DeviceLink:
    def __init__(self, port=None):
        self.port = port
        self.connection = None
        self.lock = threading.Lock()

    def close(self):
        if self.connection:
            try:
                self.connection.close()
            except OSError:
                pass
        self.connection = None

    def rpc(self, method, body=None):
        with self.lock:
            try:
                if self.connection is None:
                    ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
                    selected = self.port or (ports[0] if len(ports) == 1 else None)
                    if not selected:
                        raise OSError('请插入 ESP32 USB 数据线；检测到多块板时请指定串口')
                    connection = serial.Serial()
                    connection.port = selected
                    connection.baudrate = 115200
                    connection.timeout = 0.1
                    connection.write_timeout = 3
                    connection.dtr = True
                    connection.rts = False
                    connection.open()
                    self.connection = connection
                request_id = uuid.uuid4().hex
                message = {'type': 'usb_config', 'id': request_id, 'method': method}
                if body is not None:
                    message['body'] = body
                # HWCDC has a small receive buffer; pace long CA/config messages
                # so the device loop can consume every byte before the next batch.
                wire = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
                for offset in range(0, len(wire), 64):
                    self.connection.write(wire[offset:offset + 64])
                    time.sleep(0.01)
                deadline = time.monotonic() + 10
                pending = bytearray()
                while time.monotonic() < deadline:
                    pending.extend(self.connection.read_until(b'\n', 18000))
                    if len(pending) > 18000:
                        pending.clear()
                        continue
                    if not pending.endswith(b'\n'):
                        continue
                    line = bytes(pending)
                    pending.clear()
                    marker = line.find(b'POCKET_USB ')
                    if marker < 0:
                        continue
                    try:
                        response = json.loads(line[marker + 11:])
                    except (ValueError, UnicodeError):
                        continue
                    if response.get('id') == request_id:
                        return response['status'], response['data']
                raise OSError('设备未响应，请确认已刷入支持 USB 配置的固件，并关闭其他串口工具')
            except (OSError, serial.SerialException):
                self.close()
                raise


class ConfigServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, device, key=None):
        self.device = device
        self.key = key or secrets.token_urlsafe(32)
        super().__init__(address, ConfigHandler)


class ConfigHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log configuration bodies or browser access keys.

    def reply(self, code, data, content_type='application/json; charset=utf-8'):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def trusted_request(self, authentication=True):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            self.reply(403, {'error': '请通过本机配置地址访问'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + host for host in hosts}:
            self.reply(403, {'error': '不允许跨站访问设备配置'})
            return False
        if authentication and not hmac.compare_digest(self.headers.get('X-Setup-Key', ''), self.server.key):
            self.reply(401, {'error': '请重新运行 USB 配置工具打开页面'})
            return False
        return True

    def dispatch(self, method, body=None):
        try:
            status, data = self.server.device.rpc(method, body)
            data.update(usbProxy=True, deviceConnected=True)
            self.reply(status, data)
        except (OSError, serial.SerialException) as error:
            data = {'usbProxy': True, 'deviceConnected': False, 'message': str(error)}
            if method in ('save', 'wifi_forget', 'bluetooth_pair', 'bluetooth_forget', 'bluetooth_scan_start', 'test_sound'):
                self.reply(503, {'error': str(error)})
            else:
                self.reply(200, data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.trusted_request(authentication=path not in ('/', '/api/usb-tool')):
            return
        if path == '/':
            source = (ROOT / 'firmware/Pocket/config_portal.h').read_text(encoding='utf-8')
            page = source.split('R"HTML(', 1)[1].split(')HTML"', 1)[0]
            bootstrap = '<script>sessionStorage.setItem("pocketSetupKey",' + json.dumps(self.server.key) + ');history.replaceState(null,"",location.pathname);</script>'
            page = page.replace('<script>', bootstrap + '<script>', 1)
            self.reply(200, page.encode(), 'text/html; charset=utf-8')
        elif path == '/api/usb-tool':
            self.reply(200, {'service': 'codex-pocket-usb-config'})
        elif path == '/api/status':
            self.dispatch('status')
        elif path == '/api/config':
            self.dispatch('get')
        elif path == '/api/bluetooth/scan':
            self.dispatch('bluetooth_scan_results')
        elif path == '/api/wifi/scan':
            self.dispatch('scan_results')
        else:
            self.reply(404, {'error': '没有这个页面'})

    def do_POST(self):
        if not self.trusted_request():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 12000:
                self.reply(413, {'error': '配置内容大小不正确'})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.reply(400, {'error': '配置格式错误'})
            return
        if self.path == '/api/config':
            self.dispatch('save', body)
        elif self.path == '/api/open':
            self.dispatch('open')
        elif self.path == '/api/close':
            self.dispatch('close')
        elif self.path == '/api/bluetooth/scan':
            self.dispatch('bluetooth_scan_start')
        elif self.path == '/api/bluetooth/pair':
            self.dispatch('bluetooth_pair', body)
        elif self.path == '/api/bluetooth/forget':
            self.dispatch('bluetooth_forget')
        elif self.path == '/api/wifi/forget':
            self.dispatch('wifi_forget', body)
        elif self.path == '/api/wifi/scan':
            self.dispatch('scan_start')
        elif self.path == '/api/sound/test':
            self.dispatch('test_sound')
        elif self.path == '/api/test-upload':
            self.dispatch('test_upload')
        else:
            self.reply(404, {'error': 'USB 仅支持设备配置和状态查询'})


def watch_usb(device, url, stop):
    """Open once per connection, only after the board answers our protocol."""
    announced = False
    while not stop.wait(2):
        ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
        selected = device.port or (ports[0] if len(ports) == 1 else None)
        if not selected or selected not in ports:
            announced = False
            continue
        if announced:
            continue
        try:
            status, data = device.rpc('status')
            if status == 200 and str(data.get('firmware', '')).startswith('codex-pocket-'):
                webbrowser.open(url)
                announced = True
        except (OSError, serial.SerialException):
            pass


def main():
    parser = argparse.ArgumentParser(description='Codex Pocket USB 配置网页')
    parser.add_argument('--port', type=int, default=8788)
    parser.add_argument('--serial-port')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--auto-open', action='store_true', help='open the page when a Codex Pocket USB device connects')
    parser.add_argument('--url-file', type=Path)
    args = parser.parse_args()
    if args.port:
        existing = f'http://127.0.0.1:{args.port}/'
        try:
            with urlopen(existing + 'api/usb-tool', timeout=1) as response:
                matched = json.load(response).get('service') == 'codex-pocket-usb-config'
            if matched:
                if args.url_file:
                    args.url_file.parent.mkdir(parents=True, exist_ok=True)
                    args.url_file.write_text(existing, encoding='utf-8')
                if args.open:
                    webbrowser.open(existing)
                print('USB configuration is already running.', flush=True)
                return
        except (OSError, ValueError):
            pass
    device = DeviceLink(args.serial_port)
    try:
        server = ConfigServer(('127.0.0.1', args.port), device)
    except OSError:
        server = ConfigServer(('127.0.0.1', 0), device)
    url = f'http://127.0.0.1:{server.server_port}/'
    if args.url_file:
        args.url_file.parent.mkdir(parents=True, exist_ok=True)
        args.url_file.write_text(url, encoding='utf-8')
    print(f'USB configuration ready: http://127.0.0.1:{server.server_port}/', flush=True)
    stop = threading.Event()
    if args.auto_open:
        threading.Thread(target=watch_usb, args=(device, url, stop), daemon=True).start()
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        device.close()


if __name__ == '__main__':
    main()
