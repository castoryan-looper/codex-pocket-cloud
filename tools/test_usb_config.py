import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch
from usb_config import ConfigServer, DeviceLink, watch_usb


class UsbAutoOpenTests(unittest.TestCase):
    def test_opens_only_for_verified_device_once_per_connection(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        stop = Mock()
        stop.wait.side_effect = [False, False, False, False, True]
        link = Mock(port=None)
        link.rpc.side_effect = [(200, {'firmware': 'unrelated'}), (200, {'firmware': 'codex-pocket-0.8.0'}), (200, {'firmware': 'codex-pocket-0.8.0'})]
        port = SimpleNamespace(device='COM6', vid=0x303A)
        with patch('usb_config.list_ports.comports', side_effect=[[port], [port], [], [port]]), patch('usb_config.webbrowser.open') as opened:
            watch_usb(link, 'http://127.0.0.1:8788/', stop)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(link.rpc.call_count, 3)


class FakeDevice:
    def __init__(self):
        self.calls = []
        self.offline = False

    def rpc(self, method, body=None):
        self.calls.append((method, body))
        if self.offline:
            raise OSError('USB 已断开')
        if method == 'scan_start':
            return 200, {'state': 'scanning'}
        if method == 'scan_results':
            return 200, {'state': 'done', 'networks': [{'ssid': '测试网络', 'rssi': -45, 'channel': 1, 'secure': True}]}
        return 200, {'firmware': 'test', 'wifiConnected': True, 'connectedSsid': '测试网络'}


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
        self.assertEqual(data['connectedSsid'], '测试网络')
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

    def test_configuration_screen_controls(self):
        self.assertEqual(self.request('/api/open', 'POST', '{}')[0], 200)
        self.assertEqual(self.request('/api/close', 'POST', '{}')[0], 200)
        self.assertEqual(self.device.calls, [('open', None), ('close', None)])

    def test_bluetooth_pair_and_forget_use_config_rpc(self):
        self.assertEqual(self.request('/api/bluetooth/pair','POST','{}')[0],200)
        self.assertEqual(self.request('/api/bluetooth/forget','POST','{}')[0],200)
        self.assertEqual(self.device.calls,[('bluetooth_pair',{}),('bluetooth_forget',None)])

    def test_sound_preview_routes_and_offline_failure(self):
        self.assertEqual(self.request('/api/sound/test','POST','{}')[0],200)
        self.assertEqual(self.device.calls,[('test_sound',None)])
        self.device.offline=True
        self.assertEqual(self.request('/api/sound/test','POST','{}')[0],503)

    def test_bluetooth_discovery_and_selected_address(self):
        self.assertEqual(self.request('/api/bluetooth/scan','POST','{}')[0],200)
        self.assertEqual(self.request('/api/bluetooth/scan')[0],200)
        self.assertEqual(self.request('/api/bluetooth/pair','POST','{"address":"aa:bb:cc:dd:ee:ff"}')[0],200)
        self.assertEqual(self.device.calls,[('bluetooth_scan_start',None),('bluetooth_scan_results',None),('bluetooth_pair',{'address':'aa:bb:cc:dd:ee:ff'})])

    def test_bluetooth_offline_is_not_success(self):
        self.device.offline=True
        self.assertEqual(self.request('/api/bluetooth/pair','POST','{}')[0],503)
        self.assertEqual(self.request('/api/bluetooth/forget','POST','{}')[0],503)

    def test_forget_network_uses_configuration_rpc(self):
        self.assertEqual(self.request('/api/wifi/forget', 'POST', '{"ssid":"old-network"}')[0], 200)
        self.assertEqual(self.device.calls, [('wifi_forget', {'ssid':'old-network'})])

    def test_forget_offline_is_a_failure_not_a_success(self):
        self.device.offline = True
        self.assertEqual(self.request('/api/wifi/forget', 'POST', '{"ssid":"old-network"}')[0], 503)

    def test_wifi_scan_start_and_results(self):
        status, body = self.request('/api/wifi/scan', 'POST', '{}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['state'], 'scanning')
        status, body = self.request('/api/wifi/scan')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['networks'][0]['ssid'], '测试网络')
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
        response = 'POCKET_USB ' + json.dumps({'id': request['id'], 'status': 200, 'data': {'ssid': '测试'}}, ensure_ascii=False) + '\n'
        raw = response.encode()
        self.parts = [b'boot log\n', raw[:8], raw[8:21], raw[21:]]

    def read_until(self, *args):
        return self.parts.pop(0)


class SerialTests(unittest.TestCase):
    def test_partial_serial_response_and_boot_noise(self):
        link = DeviceLink()
        link.connection = FragmentedSerial()
        self.assertEqual(link.rpc('get'), (200, {'ssid': '测试'}))

    def test_long_config_is_sent_in_bounded_chunks(self):
        link = DeviceLink()
        fake = FragmentedSerial()
        link.connection = fake
        self.assertEqual(link.rpc('save', {'ssid':'测试', 'ca':'x'*2000}), (200, {'ssid':'测试'}))
        self.assertGreater(len(fake.chunks), 1)
        self.assertLessEqual(max(fake.chunks), 64)
        self.assertEqual(json.loads(fake.received)['body']['ssid'], '测试')


if __name__ == '__main__':
    unittest.main()
