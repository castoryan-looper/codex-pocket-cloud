import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
from usb_config import ConfigServer, DeviceLink


class FakeDevice:
    def __init__(self):
        self.calls = []
        self.offline = False

    def rpc(self, method, body=None):
        self.calls.append((method, body))
        if self.offline:
            raise OSError('USB �ѶϿ�')
        if method == 'scan_start':
            return 200, {'state': 'scanning'}
        if method == 'scan_results':
            return 200, {'state': 'done', 'networks': [{'ssid': '��������', 'rssi': -45, 'channel': 1, 'secure': True}]}
        return 200, {'firmware': 'test', 'wifiConnected': True, 'connectedSsid': '��������'}


class UsbWebTests(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.server = ConfigServer(('127.0.0.1', 0), self.device, key='test-key')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path='/api/status', method='GET', body=None, headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers={'X-Setup-Key': 'test-key', **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_status_is_real_device_data(self):
        status, body = self.request()
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data['deviceConnected'])
        self.assertTrue(data['usbProxy'])
        self.assertEqual(data['connectedSsid'], '��������')
        self.assertEqual(self.device.calls, [('status', None)])

    def test_access_key_required(self):
        self.assertEqual(self.request(headers={'X-Setup-Key': ''})[0], 401)
        self.assertFalse(self.device.calls)

    def test_foreign_origin_blocked(self):
        self.assertEqual(self.request(headers={'Origin': 'https://elsewhere.example'})[0], 403)
        self.assertFalse(self.device.calls)

    def test_dns_rebinding_host_blocked(self):
        self.assertEqual(self.request(headers={'Host': 'elsewhere.example'})[0], 403)
        self.assertFalse(self.device.calls)

    def test_offline_status_allows_reconnect_page(self):
        self.device.offline = True
        status, data = self.request()
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)['deviceConnected'])

    def test_only_config_writes_allowed(self):
        self.assertEqual(self.request('/api/event', 'POST', '{}')[0], 404)
        self.assertFalse(self.device.calls)
        self.assertEqual(self.request('/api/config', 'POST', '{"ssid":"new"}')[0], 200)
        self.assertEqual(self.device.calls, [('save', {'ssid': 'new'})])

    def test_wifi_scan_start_and_results(self):
        status, body = self.request('/api/wifi/scan', 'POST', '{}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'scanning')
        status, body = self.request('/api/wifi/scan')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['networks'][0]['ssid'], '��������')
        self.assertEqual(self.device.calls, [('scan_start', None), ('scan_results', None)])

    def test_bad_or_oversize_body_never_reaches_device(self):
        self.assertEqual(self.request('/api/config', 'POST', '[]')[0], 400)
        self.assertEqual(self.request('/api/config', 'POST', 'x' * 12001)[0], 413)
        self.assertFalse(self.device.calls)

    def test_page_contains_status_and_uses_safe_dom(self):
        status, body = self.request('/')
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn('/api/status', page)
        self.assertIn('textContent=value', page)
        self.assertNotIn('innerHTML', page)


class FragmentedSerial:
    def __init__(self):
        self.received = bytearray()
        self.chunks = []

    def write(self, data):
        self.chunks.append(len(data))
        self.received.extend(data)
        if not data.endswith(b'\n'):
            return len(data)
        request = json.loads(self.received)
        response = 'POCKET_USB ' + json.dumps({'id': request['id'], 'status': 200, 'data': {'ssid': '����'}}, ensure_ascii=False) + '\n'
        raw = response.encode()
        self.parts = [b'boot log\n', raw[:8], raw[8:21], raw[21:]]

    def read_until(self, *args):
        return self.parts.pop(0)


class SerialTests(unittest.TestCase):
    def test_partial_serial_response_and_boot_noise(self):
        link = DeviceLink()
        link.connection = FragmentedSerial()
        self.assertEqual(link.rpc('get'), (200, {'ssid': '����'}))

    def test_long_config_is_sent_in_bounded_chunks(self):
        link = DeviceLink()
        fake = FragmentedSerial()
        link.connection = fake
        self.assertEqual(link.rpc('save', {'ssid':'����', 'ca':'x'*2000}), (200, {'ssid':'����'}))
        self.assertGreater(len(fake.chunks), 1)
        self.assertLessEqual(max(fake.chunks), 64)
        self.assertEqual(json.loads(fake.received)['body']['ssid'], '����')


if __name__ == '__main__':
    unittest.main()
