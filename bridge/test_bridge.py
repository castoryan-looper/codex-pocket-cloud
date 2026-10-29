import asyncio
import base64
import json
import os
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

    def test_mulaw_standard_vectors_and_upsampling(self):
        import struct
        samples = struct.unpack('<8h', bridge.decode_mulaw8k(bytes([0x00, 0x7f, 0x80, 0xff])))
        self.assertEqual(samples, (-32124, -32124, 0, 0, 32124, 32124, 0, 0))

    async def test_compressed_audio_decodes_to_original_rate_before_transcription(self):
        source='wifi-compressed'
        await self.p.device_packet({'type':'audio_start','codec':'mulaw8k'},source)
        await self.p.device_packet({'type':'audio_chunk','data':base64.b64encode(bytes([0xff])*4000).decode()},source)
        with patch.object(self.p,'transcribe',new_callable=AsyncMock) as recognize:
            await self.p.device_packet({'type':'audio_end','bytes':16000},source)
            await asyncio.sleep(0)
            recognize.assert_awaited_once_with(bytes(16000))
        self.assertNotIn(source,self.p.audio_codecs)

    async def test_compressed_audio_rejects_incomplete_and_cancelled(self):
        source='wifi-compressed'
        with patch.object(self.p,'transcribe',new_callable=AsyncMock) as recognize:
            await self.p.device_packet({'type':'audio_start','codec':'mulaw8k'},source)
            await self.p.device_packet({'type':'audio_chunk','data':base64.b64encode(bytes([0xff])*3000).decode()},source)
            await self.p.device_packet({'type':'audio_end','bytes':13000},source)
            await self.p.device_packet({'type':'audio_start','codec':'mulaw8k'},source)
            await self.p.device_packet({'type':'audio_chunk','data':base64.b64encode(bytes([0xff])*3000).decode()},source)
            await self.p.device_packet({'type':'audio_end','bytes':12000,'cancelled':True},source)
            await asyncio.sleep(0)
            recognize.assert_not_awaited()
        self.assertNotIn(source,self.p.audio_codecs)

    async def test_unknown_audio_codec_is_rejected(self):
        await self.p.device_packet({'type':'audio_start','codec':'unsupported'},'wifi')
        self.assertNotIn('wifi',self.p.audio)
        self.assertEqual(self.p.status,'录音失败')

    async def test_model_prewarm_and_capture_share_one_model(self):
        import sys, threading, time
        model_path=Path(self.temp.name)/'model'
        model_path.mkdir();(model_path/'model.bin').touch()
        calls=[]
        def construct(*args,**kwargs):
            calls.append(1);time.sleep(.03);return object()
        fake=SimpleNamespace(WhisperModel=construct)
        with patch.dict(sys.modules,{'faster_whisper':fake}), patch.dict(os.environ,{'POCKET_MODEL_DIR':str(model_path)}):
            first,second=await asyncio.gather(asyncio.to_thread(self.p.load_whisper),asyncio.to_thread(self.p.load_whisper))
        self.assertIs(first,second)
        self.assertEqual(len(calls),1)

    async def test_recognized_voice_starts_turn_without_confirmation(self):
        self.p.rpc.call.return_value = {'turn': {'id':'voice-turn','status':'inProgress'}}
        with patch.object(self.p,'whisper_transcribe',return_value='检查项目状态'):
            await self.p.transcribe(bytes(16000))
        method, params = self.p.rpc.call.await_args.args
        self.assertEqual(method,'turn/start')
        self.assertEqual(params['input'],[{'type':'text','text':'检查项目状态'}])
        self.assertEqual(self.p.turn,'voice-turn')
        self.assertEqual(self.p.draft,'')
        self.assertFalse(self.p.voice_busy)

    async def test_voice_steers_existing_turn(self):
        self.p.turn = 'active-turn'
        with patch.object(self.p,'whisper_transcribe',return_value='先检查测试结果'):
            await self.p.transcribe(bytes(16000))
        method, params = self.p.rpc.call.await_args.args
        self.assertEqual(method,'turn/steer')
        self.assertEqual(params['expectedTurnId'],'active-turn')
        self.assertEqual(params['input'][0]['text'],'先检查测试结果')
        self.assertEqual(self.p.draft,'')

    async def test_failed_submit_preserves_text_without_automatic_retry(self):
        self.p.rpc.call.side_effect = RuntimeError('network timeout')
        with patch.object(self.p,'whisper_transcribe',return_value='请检查项目'):
            await self.p.transcribe(bytes(16000))
        self.p.rpc.call.assert_awaited_once()
        self.assertEqual(self.p.draft,'请检查项目')
        self.assertEqual(self.p.status,'语音失败')
        self.assertIn('文字已保留',self.p.detail)

    async def test_empty_transcription_does_not_submit(self):
        with patch.object(self.p,'whisper_transcribe',return_value=''):
            await self.p.transcribe(bytes(16000))
        self.p.rpc.call.assert_not_awaited()
        self.assertEqual(self.p.status,'语音失败')

    async def test_real_event_lifecycle(self):
        p = self.p
        await p.notification({'method':'turn/started','params':{'threadId':p.thread,'turn':{'id':'turn-1'}}})
        self.assertEqual(p.turn,'turn-1')
        await p.notification({'method':'item/agentMessage/delta','params':{'threadId':p.thread,'delta':'完成'}})
        await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'turn-1','status':'completed'}}})
        self.assertIsNone(p.turn)
        self.assertEqual(p.status,'已完成')
        self.assertEqual(p.detail,'完成')
        self.assertEqual(p.task['state'],'completed')
        self.assertEqual(p.task['validation'],'not_reported')
        self.assertEqual(p.task['turnId'],'turn-1')

    async def test_task_state_comes_from_events_and_approval(self):
        p=self.p
        await p.notification({'method':'turn/started','params':{'threadId':p.thread,'turn':{'id':'turn-1'}}})
        self.assertEqual(p.task['state'],'running')
        await p.notification({'id':7,'method':'item/commandExecution/requestApproval','params':{'threadId':p.thread,'command':'example'}})
        self.assertEqual(p.task['state'],'awaiting_approval')
        await p.notification({'method':'turn/completed','params':{'threadId':p.thread,'turn':{'id':'turn-1','status':'failed','error':{'message':'failed'}}}})
        self.assertEqual(p.task['state'],'failed')
        self.assertEqual(p.task['summary'],'failed')
        self.assertEqual(p.task['validation'],'not_reported')

    async def test_submission_failure_is_not_reported_as_success(self):
        self.p.rpc.call.side_effect=TimeoutError('uncertain')
        with self.assertRaises(TimeoutError): await self.p.submit('test',source='recommendation')
        self.assertEqual(self.p.task['state'],'delivery_unknown')
        self.assertEqual(self.p.task['source'],'recommendation')
        self.assertEqual(self.p.snapshot()['task']['validation'],'not_reported')

    async def test_start_and_resume_include_pocket_guidance(self):
        async def run_case(thread):
            p=self.p
            p.thread=thread
            p.rpc.start=AsyncMock()
            p.rpc.call=AsyncMock(side_effect=lambda method,params: {'account':{'type':'chatgpt'}} if method=='account/read' else {'thread':{'id':'thread-1'}} if method in ('thread/start','thread/resume') else {})
            p.refresh_limits=AsyncMock()
            p.config['speech_backend']='windows'
            with patch('bridge.asyncio.create_task',side_effect=lambda coro: coro.close()):
                await p.start()
            method='thread/resume' if thread else 'thread/start'
            args=next(call.args[1] for call in p.rpc.call.await_args_list if call.args[0]==method)
            self.assertIn('developerInstructions',args)
            self.assertIn('浏览和跳过不是授权',args['developerInstructions'])
        await run_case(None)
        await run_case('existing-thread')

    async def test_missing_session_recovers_without_replaying_a_turn(self):
        for message in ('no rollout found for thread id old', 'invalid session id: bad'):
            p=self.p
            p.thread='old'
            p.config['thread_id']='old'
            p.save()
            before=p.config_path.read_bytes()
            async def call(method, params):
                if method=='account/read': return {'account': {'type':'chatgpt'}}
                if method=='thread/resume': raise bridge.RpcError({'code':-32600,'message':message})
                if method=='thread/start': return {'thread':{'id':'new'}}
                return {}
            p.rpc.start=AsyncMock();p.rpc.call=AsyncMock(side_effect=call)
            p.refresh_limits=AsyncMock();p.config['speech_backend']='windows'
            with patch('bridge.asyncio.create_task',side_effect=lambda coro:coro.close()):
                await p.start()
            self.assertEqual(p.config['thread_id'],'new')
            self.assertEqual(p.config['thread_recovery']['previous_thread_id'],'old')
            self.assertTrue(any(f.read_bytes()==before for f in p.runtime.glob('settings.before-thread-recovery-*.json')))
            self.assertFalse(any(c.args[0]=='turn/start' for c in p.rpc.call.await_args_list))

    async def test_other_resume_errors_keep_config_and_do_not_start_thread(self):
        for error in (bridge.RpcError({'code':-32600,'message':'permission denied'}),
                      bridge.RpcError({'code':-32000,'message':'no rollout found for thread id old'}),
                      TimeoutError('timeout'), RuntimeError('connection lost')):
            p=self.p;p.thread='old';p.config['thread_id']='old';p.save();before=p.config_path.read_bytes()
            async def call(method, params):
                if method=='account/read':return {'account':{'type':'chatgpt'}}
                raise error
            p.rpc.start=AsyncMock();p.rpc.call=AsyncMock(side_effect=call)
            with self.assertRaises(RuntimeError):await p.start()
            self.assertEqual(p.config_path.read_bytes(),before)
            self.assertFalse(any(c.args[0]=='thread/start' for c in p.rpc.call.await_args_list))

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

    async def test_docker_uses_external_sandbox_only_when_configured(self):
        self.p.rpc.call.return_value = {'turn': {'id': 'test-turn', 'status': 'completed'}}
        with patch.dict(os.environ, {'POCKET_EXTERNAL_SANDBOX': 'docker'}):
            await self.p.submit('task inside the container')
        params = self.p.rpc.call.await_args.args[1]
        self.assertEqual(params['sandboxPolicy'], {'type': 'externalSandbox', 'networkAccess': 'enabled'})
        with patch.dict(os.environ, {'POCKET_EXTERNAL_SANDBOX': ''}):
            await self.p.submit('task with native sandbox defaults')
        self.assertNotIn('sandboxPolicy', self.p.rpc.call.await_args.args[1])

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

    async def test_cancelled_push_to_talk_never_starts_transcription(self):
        await self.p.device_packet({'type': 'audio_start'}, 'wifi-test')
        self.p.audio['wifi-test'].extend(b'\0' * 9600)
        with patch.object(self.p, 'transcribe', new_callable=AsyncMock) as recognize:
            await self.p.device_packet({'type': 'audio_end', 'bytes': 9600, 'cancelled': True, 'error': 'cancelled'}, 'wifi-test')
            await asyncio.sleep(0)
            recognize.assert_not_awaited()
        self.assertNotIn('wifi-test', self.p.audio)
        self.assertEqual(self.p.status, '已取消')

    async def test_new_recording_does_not_mix_with_pending_transcription(self):
        self.p.voice_busy = True
        await self.p.device_packet({'type': 'audio_start'}, 'wifi-test')
        self.assertNotIn('wifi-test', self.p.audio)
        self.p.voice_busy = False
        self.p.draft = 'previous draft'
        await self.p.device_packet({'type': 'audio_start'}, 'wifi-test')
        self.assertEqual(self.p.draft, '')
        self.assertEqual(self.p.audio['wifi-test'], bytearray())

    async def test_frames_expose_voice_state_without_changing_bitmap_protocol(self):
        peer = SimpleNamespace(send_json=AsyncMock())
        self.p.peers = {'wifi-test': peer}
        self.p.voice_busy = True
        self.p.status = '识别语音'
        with patch('bridge.asyncio.sleep', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.p.tick()
        frame = peer.send_json.await_args.args[0]
        self.assertEqual(frame['type'], 'frame')
        self.assertEqual(len(base64.b64decode(frame['data'])), 5000)
        self.assertEqual(frame['state'], '识别语音')
        self.assertTrue(frame['voiceBusy'])
        self.assertIn('mulaw8k',frame['audioCodecs'])
        self.assertTrue(frame['voiceAutoSubmit'])

    def test_unknown_quota_not_zero_and_no_fake_tokens(self):
        self.assertEqual(self.p.remaining(),[])
        self.assertIsNone(self.p.tokens)
        import time
        self.p.rate={'codex':{'primary':{'usedPercent':36,'windowDurationMins':300}}}
        self.p.rate_at=time.time()
        self.assertEqual(self.p.remaining()[0]['remaining'],64)
        self.p.rate_at=time.time()-400
        self.assertEqual(self.p.remaining(),[])

    def test_dashboard_context_is_real_context_not_subscription_tokens(self):
        self.assertEqual(bridge.dashboard_context(None), '上下文剩余：未知')
        self.assertEqual(bridge.dashboard_context({'used': 1000, 'max': 10000}), '上下文剩余：9.0k')
        self.assertEqual(bridge.dashboard_context({'used': 100, 'max': 500}), '上下文剩余：400')
        self.assertEqual(bridge.dashboard_context({'used': -1, 'max': 100}), '上下文剩余：未知')

    def test_dashboard_suggestions_use_measured_state(self):
        self.assertEqual(bridge.dashboard_advice(9, None, 10, []), '让AI整理待办，记得充电')
        self.assertEqual(bridge.dashboard_advice(9, None, 90, [{'remaining': 10}]), '让AI合并零散任务')
        self.assertEqual(bridge.dashboard_advice(14, {'temperature': 32, 'code': 0}, 90, []), '让AI安排室内任务')
        self.assertEqual(bridge.dashboard_advice(14, {'temperature': 20, 'code': 61}, 90, []), '让AI规划雨天安排')
        self.assertEqual(bridge.dashboard_advice(23, None, None, []), '让AI复盘并安排明日')

    def test_weather_expires_and_idle_dashboard_preserves_action_states(self):
        import time
        self.p.weather = {'temperature': 21.5, 'code': 0}
        self.p.weather_at = time.monotonic()-5401
        self.assertIsNone(self.p.current_weather())
        self.p.weather_at = time.monotonic()
        self.assertEqual(self.p.current_weather()['temperature'], 21.5)
        self.p.status='待命'
        self.p.device['battery']=80
        dashboard=self.p.render().tobytes()
        self.assertEqual(len(dashboard),5000)
        self.p.draft='需要保留的草稿'
        self.assertNotEqual(dashboard,self.p.render().tobytes())
        self.p.draft=''
        self.p.turn='running'
        self.p.status='工作中'
        self.assertNotEqual(dashboard,self.p.render().tobytes())

    async def test_weather_is_opt_in_without_coordinates(self):
        with patch.dict(os.environ, {'POCKET_WEATHER_LAT':'','POCKET_WEATHER_LON':''}), patch('bridge.ClientSession') as session:
            await self.p.refresh_weather()
            session.assert_not_called()
        self.assertIsNone(self.p.weather)

    async def test_weather_fetch_uses_explicit_coordinates(self):
        response=AsyncMock()
        response.raise_for_status=__import__('unittest.mock',fromlist=['Mock']).Mock()
        response.json.return_value={'current':{'temperature_2m':22.3,'weather_code':61}}
        session=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock()
        session.get.return_value.__aenter__=AsyncMock(return_value=response)
        session.get.return_value.__aexit__=AsyncMock(return_value=False)
        factory=__import__('unittest.mock',fromlist=['MagicMock']).MagicMock()
        factory.return_value.__aenter__=AsyncMock(return_value=session)
        factory.return_value.__aexit__=AsyncMock(return_value=False)
        with patch.dict(os.environ, {'POCKET_WEATHER_LAT':'31.2','POCKET_WEATHER_LON':'121.4'}), patch('bridge.ClientSession',factory):
            await self.p.refresh_weather()
        self.assertEqual(self.p.weather,{'temperature':22.3,'code':61})
        self.assertEqual(session.get.call_args.kwargs['params']['latitude'],31.2)

    def test_chat_advice_uses_only_recent_device_thread_user_messages(self):
        now=1700000000
        def turn(age,text,kind='userMessage'):
            return {'startedAt':now-age,'items':[{'type':kind,'content':[{'type':'text','text':text}]}]}
        self.assertEqual(bridge.chat_history_advice([turn(60,'帮我优化ESP32语音')],now),('聊天建议','让AI梳理设备项目进展'))
        self.assertEqual(bridge.chat_history_advice([turn(60,'整理今天的待办')],now),('聊天建议','让AI梳理聊天中的待办'))
        self.assertIsNone(bridge.chat_history_advice([turn(259201,'帮我优化ESP32')],now))
        self.assertIsNone(bridge.chat_history_advice([turn(60,'待办','agentMessage')],now))
        self.assertIsNone(bridge.chat_history_advice([{'items':[]}],now))

    async def test_chat_read_is_bounded_and_uses_device_thread_only(self):
        import time
        self.p.rpc.call.return_value={'data':[{'startedAt':time.time(),'items':[{'type':'userMessage','content':[{'type':'text','text':'聊聊我的项目'}]}]}]}
        await self.p.refresh_chat_advice()
        method,params=self.p.rpc.call.await_args.args
        self.assertEqual(method,'thread/turns/list')
        self.assertEqual(params['threadId'],'test-thread')
        self.assertEqual(params['limit'],12)
        self.assertEqual(params['sortDirection'],'desc')
        self.assertIsNone(self.p.current_chat_advice())

    def test_personal_advice_requires_fresh_explicit_data(self):
        now=1700000000
        self.assertIsNone(bridge.personal_advice({},now))
        self.assertIsNone(bridge.personal_advice({'updatedAt':now-21601,'todos':[{'title':'过期待办'}]},now))
        self.assertIsNone(bridge.personal_advice({'updatedAt':now+301,'todos':[{'title':'未来数据'}]},now))
        data={'updatedAt':now,'calendar':[{'title':'项目会议','startAt':now+1800}],
              'todos':[{'title':'待办一'}],'projects':[{'title':'项目一'}],'interests':[{'title':'话题一'}]}
        self.assertEqual(bridge.personal_advice(data,now),('日程建议','让AI准备项目会议'))
        data['recommendation']={'text':'让AI整理会前材料','basedOn':['calendar','projects']}
        self.assertEqual(bridge.personal_advice(data,now),('综合建议','让AI整理会前材料'))
        data.pop('recommendation')
        data['calendar'][0]['startAt']=now+10000
        self.assertEqual(bridge.personal_advice(data,now),('待办建议','让AI推进待办一'))
        data['todos']=[]
        self.assertEqual(bridge.personal_advice(data,now),('项目建议','让AI梳理项目一'))
        data['projects']=[]
        self.assertEqual(bridge.personal_advice(data,now),('兴趣建议','让AI调研话题一'))

    async def test_dashboard_context_requires_auth_and_never_starts_task(self):
        from aiohttp.test_utils import TestClient,TestServer
        async with TestClient(TestServer(self.p.app())) as client:
            data={'todos':[{'title':'准备发布'}], 'recommendation':{'text':'让AI整理发布清单','basedOn':['todos']}}
            response=await client.post('/api/dashboard-context',json=data)
            self.assertEqual(response.status,401)
            headers={'Authorization':'Bearer '+self.p.config['console_token']}
            response=await client.post('/api/dashboard-context',headers=headers,json=data)
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['counts']['todos'],1)
            stored=self.p.read_personal_context()
            self.assertEqual(stored['todos'],[{'title':'准备发布'}])
            self.assertEqual(stored['recommendation']['text'],'让AI整理发布清单')
            self.p.rpc.call.assert_not_awaited()
            response=await client.post('/api/dashboard-context',headers=headers,json={'calendar':[{'title':'bad','startAt':True}]})
            self.assertEqual(response.status,400)
            self.assertEqual(self.p.read_personal_context()['todos'],[{'title':'准备发布'}])

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
