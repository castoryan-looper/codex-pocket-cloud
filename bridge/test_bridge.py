import asyncio
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import bridge


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = bridge.ROOT
        bridge.ROOT = Path(self.temp.name)
        self.p = bridge.Pocket(SimpleNamespace(port='auto'))
        self.p.rpc = SimpleNamespace(call=AsyncMock(), send=AsyncMock())
        self.p.thread = 'test-thread'

    def tearDown(self):
        bridge.ROOT = self.old_root
        self.temp.cleanup()

    async def test_real_event_lifecycle(self):
        p = self.p
        await p.notification({'method':'turn/started','params':{'threadId':p.thread,'turn':{'id':'turn-1'}}})
        self.assertEqual(p.turn,'turn-1')
        await p.notification({'method':'item/agentMessage/delta','params':{'threadId':p.thread,'delta':'完成'}})
        await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'turn-1','status':'completed'}}})
        self.assertIsNone(p.turn)
        self.assertEqual(p.status,'已完成')
        self.assertEqual(p.detail,'完成')

    async def test_foreign_thread_ignored(self):
        await self.p.notification({'method':'turn/started','params':{'threadId':'another','turn':{'id':'wrong'}}})
        self.assertIsNone(self.p.turn)

    async def test_approval_is_not_auto_accepted(self):
        request={'id':99,'method':'item/commandExecution/requestApproval','params':{'threadId':self.p.thread,'command':'test command'}}
        await self.p.notification(request)
        self.p.rpc.send.assert_not_awaited()
        self.assertEqual(self.p.status,'等待确认')
        await self.p.action('decline')
        self.p.rpc.send.assert_awaited_once_with({'id':99,'result':{'decision':'decline'}})

    async def test_no_parallel_turns(self):
        self.p.turn='busy'
        with self.assertRaises(ValueError):
            await self.p.submit('new task')
        self.p.rpc.call.assert_not_awaited()

    async def test_stop_uses_actual_turn(self):
        self.p.turn='active-turn'
        await self.p.action('b2_long')
        self.p.rpc.call.assert_awaited_once_with('turn/interrupt',{'threadId':'test-thread','turnId':'active-turn'})

    async def test_audio_bounds_and_confirmation(self):
        p=self.p
        await p.device_packet({'type':'audio_start'},'usb')
        p.audio['usb']=bytearray(640000)
        await p.device_packet({'type':'audio_chunk','data':base64.b64encode(b'xx').decode()},'usb')
        self.assertNotIn('usb',p.audio)
        self.assertEqual(p.status,'录音失败')
        await p.action('draft',{'text':'修改一个文件'})
        self.assertEqual(p.draft,'修改一个文件')
        p.rpc.call.assert_not_awaited()

    def test_unknown_quota_not_zero_and_no_fake_tokens(self):
        self.assertEqual(self.p.remaining(),[])
        self.assertIsNone(self.p.tokens)
        import time
        self.p.rate={'codex':{'primary':{'usedPercent':36,'windowDurationMins':300}}}
        self.p.rate_at=time.time()
        self.assertEqual(self.p.remaining()[0]['remaining'],64)
        self.p.rate_at=time.time()-400
        self.assertEqual(self.p.remaining(),[])

    def test_screen_wire_layout(self):
        image=self.p.render()
        self.assertEqual(image.size,(200,200))
        self.assertEqual(len(image.tobytes()),5000)

    async def test_console_auth_and_origin(self):
        from aiohttp import web
        p=self.p
        def request(remote='127.0.0.1',headers=None):
            return SimpleNamespace(remote=remote,headers=headers or {},scheme='http',host='127.0.0.1:8787')
        with self.assertRaises(web.HTTPUnauthorized):
            p.check_console(request())
        with self.assertRaises(web.HTTPForbidden):
            p.check_console(request(remote='192.168.1.5'))
        good={'Authorization':'Bearer '+p.config['console_token']}
        p.check_console(request(headers=good))
        with self.assertRaises(web.HTTPForbidden):
            p.check_console(request(headers={**good,'Origin':'https://evil.example'}))
        with self.assertRaises(web.HTTPForbidden):
            p.check_console(request(headers={**good,'X-Forwarded-For':'8.8.8.8'}))

    async def test_unsupported_server_request_denied(self):
        await self.p.notification({'id':3,'method':'item/permissions/requestApproval','params':{'threadId':self.p.thread}})
        msg=self.p.rpc.send.await_args.args[0]
        self.assertIn('error',msg)

    async def test_short_recording_leaves_recording_state(self):
        self.p.status='录音中'
        await self.p.transcribe(b'\0'*1000)
        self.assertEqual(self.p.status,'录音太短')
        self.assertFalse(self.p.voice_busy)

    async def test_incomplete_audio_is_rejected(self):
        await self.p.device_packet({'type':'audio_start'},'usb')
        self.p.audio['usb'].extend(b'\0'*1024)
        await self.p.device_packet({'type':'audio_end','bytes':2048},'usb')
        self.assertEqual(self.p.status,'录音失败')
        self.assertIn('传输不完整',self.p.detail)

    def test_battery_noise_does_not_change_screen(self):
        self.p.device['battery']=90
        first=self.p.render().tobytes()
        self.p.device['battery']=91
        self.assertEqual(first,self.p.render().tobytes())
        self.p.device['battery']=92
        self.assertNotEqual(first,self.p.render().tobytes())

    async def test_usb_provisioning_removed_and_export_requires_auth(self):
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            response=await client.post('/api/provision',json={})
            self.assertEqual(response.status,404)
            response=await client.get('/api/connection')
            self.assertEqual(response.status,401)
            response=await client.get('/api/connection',headers={'Authorization':'Bearer '+self.p.config['console_token']})
            self.assertEqual(response.status,200)
            package=await response.json()
            self.assertEqual(package['token'],self.p.config['device_token'])
            self.assertNotIn('password',package)

    def test_bridge_online_requires_live_peer_and_recent_frame(self):
        import time
        self.p.device['frameAck']=time.time()
        self.assertFalse(self.p.snapshot()['device']['bridgeConnected'])
        self.p.peers['test']=object()
        self.assertTrue(self.p.snapshot()['device']['bridgeConnected'])
        self.p.device['frameAck']=time.time()-30
        self.assertFalse(self.p.snapshot()['device']['bridgeConnected'])


if __name__=='__main__':
    unittest.main()
