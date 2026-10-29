import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock

import bridge

class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name).resolve()
        self.environment=patch.dict(os.environ,{'POCKET_STATE_DIR':str(self.directory/'state'),'POCKET_WORKSPACE':str(self.directory/'workspace'),'POCKET_PUBLIC_HOST':'pocket.example.com','POCKET_PUBLIC_PORT':'443'},clear=False)
        self.environment.start()
        self.p=bridge.Pocket(SimpleNamespace())
        self.p.rpc=SimpleNamespace(process=SimpleNamespace(returncode=None),call=AsyncMock(),send=AsyncMock())

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_state_is_outside_source_and_keys_survive_restart(self):
        self.assertEqual(self.p.runtime,self.directory/'state')
        second=bridge.Pocket(SimpleNamespace())
        self.assertEqual(self.p.config['device_token'],second.config['device_token'])
        self.assertEqual(self.p.config['cwd'],str(self.directory/'workspace'))

    async def test_public_health_is_minimal_and_tracks_server_exit(self):
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            r=await client.get('/healthz')
            self.assertEqual(r.status,200)
            self.assertEqual(set((await r.json()).keys()),{'ok','deviceConnected','commit'})
            self.assertEqual(r.headers.get('Cache-Control'),'no-store')
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'bridge'
                root.mkdir()
                (root.parent/'deployment-revision.txt').write_text('a'*40,encoding='ascii')
                with patch.object(bridge,'ROOT',root):
                    r=await client.get('/healthz')
                    self.assertEqual((await r.json())['commit'],'a'*40)
                    (root.parent/'deployment-revision.txt').write_text('$Format:%H$',encoding='ascii')
                    r=await client.get('/healthz')
                    self.assertIsNone((await r.json())['commit'])
            self.p.rpc.process.returncode=1
            r=await client.get('/healthz')
            self.assertEqual(r.status,503)

    async def test_server_export_uses_public_domain_and_private_auth(self):
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            r=await client.get('/api/connection',headers={'Authorization':'Bearer '+self.p.config['console_token']})
            self.assertEqual(r.status,200)
            config=await r.json()
            self.assertEqual(config['host'],'pocket.example.com')
            self.assertEqual(config['path'],'/device')
            self.assertTrue(config['tls'])
            self.assertEqual(config['token'],self.p.config['device_token'])
            r=await client.get('/api/connection',headers={'Authorization':'Bearer '+self.p.config['console_token'],'Host':'public.example.com'})
            self.assertEqual(r.status,403)

    async def test_websocket_protocol_auth(self):
        from aiohttp.test_utils import TestClient,TestServer
        from aiohttp import WSMsgType
        async with TestClient(TestServer(self.p.app())) as client:
            ws=await client.ws_connect('/device',protocols=('arduino',))
            await ws.send_json({'type':'hello','token':'wrong'})
            msg=await ws.receive(timeout=2)
            self.assertEqual(msg.type,WSMsgType.CLOSE)
            self.assertEqual(msg.data,1008)
            ws=await client.ws_connect('/device',protocols=('arduino',))
            await ws.send_json({'type':'hello','token':self.p.config['device_token'],'wifiConnected':True})
            # API state after the hello must report the authenticated Wi-Fi client.
            import asyncio
            await asyncio.sleep(0.05)
            self.assertEqual(self.p.device['source'],'wifi')
            self.assertTrue(self.p.device['wifiConnected'])
            await ws.close()

    async def test_websocket_compressed_audio_is_decoded_before_recognition(self):
        import asyncio, base64
        from unittest.mock import AsyncMock
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            with patch.object(self.p,'transcribe',new_callable=AsyncMock) as recognize:
                ws=await client.ws_connect('/device',protocols=('arduino',))
                await ws.send_json({'type':'hello','token':self.p.config['device_token']})
                await ws.send_json({'type':'audio_start','codec':'mulaw8k'})
                await ws.send_json({'type':'audio_chunk','data':base64.b64encode(bytes([255])*4000).decode()})
                await ws.send_json({'type':'audio_end','bytes':16000})
                for _ in range(20):
                    if recognize.await_count:break
                    await asyncio.sleep(.02)
                recognize.assert_awaited_once_with(bytes(16000))
                await ws.close()

    async def test_disconnect_discards_partial_audio_before_retry(self):
        import asyncio, base64
        from unittest.mock import AsyncMock
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            with patch.object(self.p,'transcribe',new_callable=AsyncMock) as recognize:
                ws=await client.ws_connect('/device',protocols=('arduino',))
                await ws.send_json({'type':'hello','token':self.p.config['device_token']})
                await ws.send_json({'type':'audio_start','codec':'mulaw8k'})
                await ws.send_json({'type':'audio_chunk','data':base64.b64encode(bytes([255])*1000).decode()})
                for _ in range(20):
                    if self.p.audio and len(next(iter(self.p.audio.values())))==1000:break
                    await asyncio.sleep(.02)
                self.assertTrue(self.p.audio)
                await ws.close()
                for _ in range(20):
                    if not self.p.audio:break
                    await asyncio.sleep(.02)
                self.assertFalse(self.p.audio)
                self.assertFalse(self.p.audio_codecs)
                ws=await client.ws_connect('/device',protocols=('arduino',))
                await ws.send_json({'type':'hello','token':self.p.config['device_token']})
                await ws.send_json({'type':'audio_start','codec':'mulaw8k'})
                await ws.send_json({'type':'audio_chunk','data':base64.b64encode(bytes([255])*4000).decode()})
                await ws.send_json({'type':'audio_end','bytes':16000})
                for _ in range(20):
                    if recognize.await_count:break
                    await asyncio.sleep(.02)
                recognize.assert_awaited_once_with(bytes(16000))
                await ws.close()

    async def test_voice_upload_recognizes_and_submits_without_confirmation(self):
        import asyncio, base64
        from aiohttp.test_utils import TestClient,TestServer
        self.p.thread='hardware-thread'
        self.p.rpc.call.return_value={'turn':{'id':'new-turn','status':'inProgress'}}
        async with TestClient(TestServer(self.p.app())) as client:
            with patch.object(self.p,'whisper_transcribe',return_value='检查项目'):
                ws=await client.ws_connect('/device',protocols=('arduino',))
                await ws.send_json({'type':'hello','token':self.p.config['device_token']})
                await ws.send_json({'type':'audio_start','codec':'mulaw8k'})
                await ws.send_json({'type':'audio_chunk','data':base64.b64encode(bytes([255])*4000).decode()})
                await ws.send_json({'type':'audio_end','bytes':16000})
                for _ in range(100):
                    if self.p.rpc.call.await_count:break
                    await asyncio.sleep(.01)
                self.p.rpc.call.assert_awaited_once()
                method,params=self.p.rpc.call.await_args.args
                self.assertEqual(method,'turn/start')
                self.assertEqual(params['input'],[{'type':'text','text':'检查项目'}])
                self.assertEqual(self.p.draft,'')
                await ws.close()

    def test_font_override_is_explicit(self):
        test_font=self.directory/'font.ttc';test_font.touch()
        with patch.dict(os.environ,{'POCKET_FONT':str(test_font)}):
            self.assertEqual(bridge.font_path(),test_font)
        with patch.dict(os.environ,{'POCKET_FONT':str(self.directory/'missing.ttc')}):
            with self.assertRaises(RuntimeError):bridge.font_path()

if __name__=='__main__':unittest.main()

class ConnectionExportTests(unittest.TestCase):
    def test_ip_export_preserves_custom_port_and_tls(self):
        import runpy
        module=runpy.run_path(str(Path(__file__).resolve().parents[1]/'deploy/export_connection.py'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'settings.json').write_text(json.dumps({'device_token':'test-token'}))
            (root/'ca.pem').write_text('test-ca')
            with patch.dict(os.environ,{'POCKET_STATE_DIR':str(root),'POCKET_CA_FILE':str(root/'ca.pem'),'POCKET_PUBLIC_HOST':'43.160.215.253','POCKET_PUBLIC_PORT':'8443'}):
                package=module['connection']()
            self.assertEqual(package,{'host':'43.160.215.253','port':8443,'tls':True,'path':'/device','token':'test-token','ca':'test-ca'})
