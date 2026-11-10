import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from voxi_agent import AgentHub
from voxi_agent_store import AgentStore, DEFAULTS
from voxi_memory import MemoryStore
from voxi import VoxiRadio, VoiceSession

USER='a'*32
OTHER='b'*32


class Rpc:
    def __init__(self, notify):
        self.notify=notify;self.calls=[];self.sent=[];self.counter=0;self.turns=0
    async def start(self):pass
    async def close(self):pass
    async def send(self, msg):self.sent.append(msg)
    async def call(self, method, params=None):
        self.calls.append((method,params))
        if method=='account/read':return {'account':{'type':'chatgpt'}}
        if method=='thread/start':self.counter+=1;return {'thread':{'id':'thread-'+str(self.counter)}}
        if method=='thread/resume':return {'thread':{'id':params['threadId']}}
        if method=='turn/start':
            self.turns+=1
            return {'turn':{'id':'turn-'+str(self.turns),'status':'inProgress'}}
        return {}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=AgentStore(self.temp.name)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_request_id_is_durable_and_never_changes_meaning(self):
        task,created=self.store.submit(USER,'device',1,'request','do work')
        same,again=self.store.submit(USER,'device',1,'request','do work')
        self.assertTrue(created);self.assertFalse(again);self.assertEqual(task['id'],same['id'])
        with self.assertRaises(ValueError):self.store.submit(USER,'device',1,'request','different work')
        self.assertIsNone(self.store.task(task['id'],OTHER))
        self.store.update(task['id'],state='running',thread_id='thread',turn_id='turn')
        self.store.close();self.store=AgentStore(self.temp.name)
        self.assertEqual(self.store.task(task['id'])['state'],'interrupted')
        self.assertEqual(self.store.task(task['id'])['turn_id'],'turn')
    def test_credentials_are_encrypted_and_never_returned(self):
        key='sk-fixture-never-a-real-key'
        self.store.save_secret('openai',key)
        self.assertTrue(self.store.credentials()['openai']['configured'])
        self.assertNotIn(key,json.dumps(self.store.credentials()))
        row=self.store.db.execute('SELECT ciphertext FROM secrets').fetchone()[0]
        self.assertNotIn(key,row)
        self.assertFalse(self.store.credentials()['openai']['active'])
        self.store.save_secret('openai','')
        self.assertFalse(self.store.credentials()['openai']['configured'])
    def test_user_channels_override_defaults_without_crossing_users(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True
        self.store.save_settings(cfg)
        channels=copy.deepcopy(cfg['channels']);channels[1]['name']='我的项目'
        self.store.save_settings(channels,USER)
        self.assertEqual(self.store.settings(USER)['channels'][1]['name'],'我的项目')
        self.assertEqual(self.store.settings(OTHER)['channels'][1]['name'],'Voxi Agent')

    def test_old_shared_channels_yield_to_fixed_defaults_and_preserve_personal_channels(self):
        legacy = {**DEFAULTS, 'enabled': True, 'instructions': '旧版自定义规则',
                  'channels': [{'name': '旧默认助手', 'mode': 'agent'}]}
        with self.store.db:
            self.store.db.execute('INSERT INTO config VALUES (?, ?)', ('settings', json.dumps(legacy)))
        custom = [{'slot': 7, 'name': '我的助手', 'mode': 'agent', 'voice': 'cove'}]
        self.store.save_settings(custom, USER)
        self.assertEqual(self.store.settings(OTHER),
                         {**legacy, 'channels': [{'name': '快问', 'mode': 'fast'}, {'name': 'Voxi Agent', 'mode': 'agent'}]})
        self.assertEqual(self.store.settings(USER)['channels'], custom)
        self.store.reset_channels(USER)
        self.assertEqual(self.store.settings(USER)['channels'], self.store.settings(OTHER)['channels'])


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.hub=AgentHub(self.temp.name,Path(self.temp.name)/'workspace',Rpc)
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True;self.hub.store.save_settings(cfg)
    async def asyncTearDown(self):await self.hub.close();self.temp.cleanup()
    async def running(self, ident):
        for _ in range(100):
            task=self.hub.store.task(ident)
            if task['state']=='running':return task
            await asyncio.sleep(.01)
        self.fail('task did not reach running')
    async def complete(self,task,text='完成，已验证'):
        await self.hub.notification({'method':'item/completed','params':{'threadId':task['thread_id'],'item':{'type':'agentMessage','text':text}}})
        await self.hub.notification({'method':'turn/completed','params':{'threadId':task['thread_id'],'turn':{'id':task['turn_id'],'status':'completed'}}})
    async def test_task_survives_radio_disconnect_and_uses_event_completion(self):
        task,_=self.hub.submit(USER,None,1,'request','inspect project')
        task=await self.running(task['id'])
        listener=AsyncMock(side_effect=ConnectionError());self.hub.listeners.add(listener)
        await self.hub.notification({'method':'item/agentMessage/delta','params':{'threadId':task['thread_id'],'delta':'Everything is complete'}})
        self.assertEqual(self.hub.store.task(task['id'])['state'],'running')
        await self.complete(task)
        self.assertEqual(self.hub.store.task(task['id'])['state'],'completed')
        self.assertEqual(self.hub.store.task(task['id'])['result'],'完成，已验证')
        self.assertIn(USER,self.hub.rpc.calls[1][1]['cwd'])

    async def test_channel_role_workflow_and_mcp_are_applied_and_tool_changes_require_new_thread(self):
        channels=[{'slot':1,'name':'调研','mode':'agent','role':'请先列结论',
                   'skills':['research'],'mcp':['openai-docs']}]
        self.hub.store.save_settings(channels,USER)
        task,_=self.hub.submit(USER,None,1,'personal-tools','检查文档')
        task=await self.running(task['id'])
        start=next(params for method,params in self.hub.rpc.calls if method=='thread/start')
        self.assertTrue(start['config']['mcp_servers']['voxi_openai_docs']['enabled'])
        self.assertEqual(start['approvalPolicy'],'on-request')
        turn=next(params for method,params in self.hub.rpc.calls if method=='turn/start')
        self.assertIn('请先列结论',turn['input'][0]['text'])
        self.assertIn('可核对的来源',turn['input'][0]['text'])
        self.assertEqual(turn['input'][1]['text'],'检查文档')
        await self.complete(task)
        other,_=self.hub.submit(OTHER,None,1,'other-tools','普通任务')
        other=await self.running(other['id'])
        other_start=[params for method,params in self.hub.rpc.calls if method=='thread/start'][-1]
        self.assertFalse(other_start['config']['mcp_servers']['voxi_openai_docs']['enabled'])
        await self.complete(other)
        self.hub.store.save_settings([{**channels[0],'mcp':[]}],USER)
        with self.assertRaisesRegex(ValueError,'MCP 配置已改变'):
            await self.hub.action(USER,task['id'],'continue',text='继续',request_id='mcp-revoked')
    async def test_approval_stays_scoped_and_cancellation_waits_for_confirmation(self):
        task,_=self.hub.submit(USER,None,1,'request','edit file');task=await self.running(task['id'])
        request={'id':99,'method':'item/commandExecution/requestApproval','params':{'threadId':task['thread_id'],'command':'test command'}}
        await self.hub.notification(request)
        self.assertEqual(self.hub.store.task(task['id'])['state'],'awaiting_approval')
        with self.assertRaises(ValueError):await self.hub.action(OTHER,task['id'],'approve')
        await self.hub.action(USER,task['id'],'approve')
        self.assertEqual(self.hub.rpc.sent[-1],{'id':99,'result':{'decision':'accept'}})
        await self.hub.action(USER,task['id'],'cancel')
        self.assertEqual(self.hub.store.task(task['id'])['state'],'cancelling')
        await self.hub.notification({'method':'turn/completed','params':{'threadId':task['thread_id'],'turn':{'id':task['turn_id'],'status':'interrupted'}}})
        self.assertEqual(self.hub.store.task(task['id'])['state'],'cancelled')

    async def test_late_usage_is_aggregated_once_for_the_finished_turn(self):
        task,_=self.hub.submit(USER,None,1,'usage-test','inspect')
        task=await self.running(task['id'])
        await self.complete(task)
        self.assertIsNone(self.hub.store.product_metrics()['totalTokens'])
        event={'method':'thread/tokenUsage/updated','params':{
            'threadId':task['thread_id'],'turnId':task['turn_id'],
            'tokenUsage':{'last':{'inputTokens':100,'outputTokens':20,'totalTokens':120},'total':{'totalTokens':480}}}}
        await self.hub.notification(event)
        await self.hub.notification(event)
        stats=self.hub.store.product_metrics()
        self.assertEqual((stats['completed'],stats['threadsWithUsage'],stats['totalTokens']),(1,1,480))
        self.hub.store.record_thread_usage(task['thread_id'],{'totalTokens':200})
        self.hub.store.record_thread_usage('unrelated-thread',{'totalTokens':999})
        self.assertEqual(self.hub.store.product_metrics()['totalTokens'],480)
    async def test_submit_timeout_is_not_replayed(self):
        rpc=await self.hub.ensure_rpc();original=rpc.call
        async def call(method,params=None):
            if method=='turn/start':raise TimeoutError()
            return await original(method,params)
        rpc.call=call
        task,_=self.hub.submit(USER,None,1,'request','work')
        for _ in range(100):
            if self.hub.store.task(task['id'])['state']=='delivery_unknown':break
            await asyncio.sleep(.01)
        same,new=self.hub.submit(USER,None,1,'request','work')
        self.assertFalse(new);self.assertEqual(same['state'],'delivery_unknown')
    async def test_continue_resumes_only_an_owned_finished_thread(self):
        task,_=self.hub.submit(USER,None,1,'r1','work');task=await self.running(task['id']);await self.complete(task)
        follow=await self.hub.action(USER,task['id'],'continue',text='continue',request_id='r2')
        await self.running(follow['id'])
        self.assertIn(('thread/resume',next(p for m,p in self.hub.rpc.calls if m=='thread/resume')),self.hub.rpc.calls)
        self.assertEqual(self.hub.store.task(follow['id'])['thread_id'],task['thread_id'])
    async def test_same_channel_serializes_tasks_and_queued_cancel_never_executes(self):
        first,_=self.hub.submit(USER,None,1,'r1','work');await self.running(first['id'])
        second,_=self.hub.submit(USER,None,1,'r2','other work')
        await self.hub.action(USER,second['id'],'cancel')
        self.assertEqual(self.hub.store.task(second['id'])['state'],'cancelled')
        self.assertEqual(self.hub.rpc.turns,1)
    async def test_cancel_during_thread_creation_prevents_turn_submission(self):
        rpc=await self.hub.ensure_rpc();original=rpc.call;entered=asyncio.Event();release=asyncio.Event()
        async def call(method,params=None):
            if method=='thread/start':entered.set();await release.wait()
            return await original(method,params)
        rpc.call=call
        task,_=self.hub.submit(USER,None,1,'r1','work')
        await asyncio.wait_for(entered.wait(),1)
        await self.hub.action(USER,task['id'],'cancel');release.set();await asyncio.sleep(.03)
        self.assertEqual(self.hub.store.task(task['id'])['state'],'cancelled')
        self.assertEqual(rpc.turns,0)
    async def test_terminal_event_during_approval_send_cannot_resurrect_task(self):
        task,_=self.hub.submit(USER,None,1,'r1','work');task=await self.running(task['id'])
        await self.hub.notification({'id':9,'method':'item/fileChange/requestApproval','params':{'threadId':task['thread_id']}})
        async def send(_):await self.complete(task)
        self.hub.rpc.send=send
        result=await self.hub.action(USER,task['id'],'approve')
        self.assertEqual(result['state'],'completed')
    async def test_old_turn_replay_cannot_complete_a_continuation_being_submitted(self):
        first,_=self.hub.submit(USER,None,1,'r1','work');first=await self.running(first['id']);await self.complete(first)
        original=self.hub.rpc.call
        async def call(method,params=None):
            if method=='turn/start':
                await self.hub.notification({'method':'turn/started','params':{'threadId':first['thread_id'],'turn':{'id':first['turn_id']}}})
                await self.hub.notification({'method':'turn/completed','params':{'threadId':first['thread_id'],'turn':{'id':first['turn_id'],'status':'completed'}}})
            return await original(method,params)
        self.hub.rpc.call=call
        follow=await self.hub.action(USER,first['id'],'continue',text='continue',request_id='r2')
        current=await self.running(follow['id'])
        self.assertNotEqual(current['turn_id'],first['turn_id'])
    async def test_parallel_approval_requests_are_presented_and_resolved_one_at_a_time(self):
        task,_=self.hub.submit(USER,None,1,'r1','work');task=await self.running(task['id'])
        for ident in (11,12):
            await self.hub.notification({'id':ident,'method':'item/fileChange/requestApproval','params':{'threadId':task['thread_id']}})
        self.assertEqual(self.hub.store.approval(task['id'])['id'],11)
        first=await self.hub.action(USER,task['id'],'approve')
        self.assertEqual(first['state'],'awaiting_approval')
        self.assertEqual(self.hub.store.approval(task['id'])['id'],12)
        await self.hub.action(USER,task['id'],'decline')
        self.assertEqual([p['id'] for p in self.hub.rpc.sent],[11,12])
        self.assertIsNone(self.hub.store.approval(task['id']))


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        owner=SimpleNamespace(runtime=Path(self.temp.name),config={'device_token':'internal-only','cwd':str(Path(self.temp.name)/'workspace')})
        self.radio=VoxiRadio(owner,'unused')
        self.radio.agent.rpc_factory=Rpc
        self.uid=self.radio.memory.bind('esp32-111111111111')
        app=web.Application();app.router.add_get('/voxi/agent',self.radio.agent_configuration);app.router.add_post('/voxi/agent',self.radio.agent_configuration)
        app.router.add_post('/voxi/memory',self.radio.memory_configuration)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.headers={'Authorization':'Bearer internal-only'}
    async def asyncTearDown(self):await self.client.close();await self.radio.agent.close();self.temp.cleanup()
    async def test_endpoint_requires_internal_auth_and_redacts_saved_credentials(self):
        self.assertEqual((await self.client.get('/voxi/agent')).status,403)
        self.assertEqual((await self.client.get('/voxi/agent',headers={**self.headers,'Origin':'https://browser'})).status,403)
        response=await self.client.get('/voxi/agent',headers=self.headers);data=await response.json()
        self.assertEqual(data['users'][0]['id'],self.uid)
        self.assertEqual(data['runtime']['credentialMode'],'existing-codex-login')
        key='sk-never-a-real-fixture-key'
        response=await self.client.post('/voxi/agent',headers=self.headers,json={'action':'credential','provider':'openai','key':key})
        text=await response.text();self.assertNotIn(key,text);self.assertTrue(json.loads(text)['credentials']['openai']['configured'])
    async def test_submit_uses_bound_user_and_returns_saved_task_before_execution(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True
        await self.client.post('/voxi/agent',headers=self.headers,json={'action':'settings','settings':cfg})
        response=await self.client.post('/voxi/agent',headers=self.headers,json={'action':'submit','userId':self.uid,'channel':1,'requestId':'r','text':'work'})
        self.assertEqual(response.status,200);task=(await response.json())['task'];self.assertEqual(task['state'],'queued')
        response=await self.client.post('/voxi/agent',headers=self.headers,json={'action':'detail','userId':OTHER,'taskId':task['id']})
        self.assertEqual(response.status,400)

    async def test_admin_model_connection_is_verified_owner_scoped_and_write_only(self):
        other=self.radio.memory.create_user('Another user')['id']
        key='never-a-real-model-api-key'
        body={'action':'connect_model','userId':self.uid,'provider':'deepseek','name':'DeepSeek','model':'deepseek-flash','key':key}
        async def post(value):
            return await self.client.post('/voxi/agent',headers=self.headers,json=value)
        self.assertEqual((await self.client.post('/voxi/agent',json=body)).status,403)
        with patch.object(self.radio.agent.models,'complete',new_callable=AsyncMock) as check:
            self.assertEqual((await post({**body,'userId':OTHER})).status,400)
            response=await post(body);self.assertEqual(response.status,200)
            ident=(await response.json())['connectionId']
            probe={'action':'test_model','userId':self.uid,'connectionId':ident}
            self.assertEqual((await post({**probe,'userId':other})).status,400)
            self.assertEqual((await post(probe)).status,200)
            self.assertEqual(check.await_count,2)
        response=await self.client.get('/voxi/agent',headers=self.headers)
        self.assertNotIn(key,await response.text())
        self.assertEqual((await response.json())['modelConnections'][0]['user_id'],self.uid)
        routes=[{'slot':0,'name':'快问','mode':'fast'},{'slot':1,'name':'DeepSeek','mode':'model','connectionId':ident}]
        self.assertEqual((await post({'action':'channels','userId':other,'channels':routes})).status,400)
        self.assertEqual((await post({'action':'channels','userId':self.uid,'channels':routes})).status,200)
        remove={**probe,'action':'delete_model'}
        self.assertEqual((await post(remove)).status,400)
        self.assertEqual((await post({'action':'reset_channels','userId':self.uid})).status,200)
        self.assertEqual((await post(remove)).status,200)

    async def test_admin_user_detail_is_scoped_to_owner_and_selected_section(self):
        device='esp32-111111111111';other_device='esp32-222222222222'
        other=self.radio.memory.bind(other_device)
        for uid,identifier,mark in [(self.uid,device,'first'),(other,other_device,'second')]:
            self.radio.memory.record(uid,identifier,mark,mark+' private transcript')
            self.radio.agent.store.submit(uid,identifier,1,mark,mark+' task')
        payload={'action':'user_detail','userId':self.uid,'section':'memory'}
        self.assertEqual((await self.client.post('/voxi/memory',json=payload)).status,403)
        response=await self.client.post('/voxi/memory',headers=self.headers,json=payload)
        memory=await response.json()
        self.assertEqual([d['id'] for d in memory['devices']],[device])
        self.assertEqual([t['text'] for t in memory['turns']],['first private transcript'])
        self.assertNotIn('agent',memory)
        response=await self.client.post('/voxi/memory',headers=self.headers,json={**payload,'section':'tasks'})
        tasks=await response.json()
        self.assertEqual([t['user_id'] for t in tasks['agent']['tasks']],[self.uid])
        self.assertNotIn('turns',tasks)
        response=await self.client.post('/voxi/memory',headers=self.headers,json={'action':'directory'})
        directory=(await response.json())['profiles']
        self.assertEqual({p['id']:p['deviceCount'] for p in directory},{self.uid:1,other:1})
        self.assertTrue(all('profile' not in p for p in directory))

    async def test_settings_patch_saves_only_requested_fields(self):
        original = self.radio.agent.store.settings()
        for patch in ({'enabled': True}, {'instructions': '先给简洁结论。'}):
            response = await self.client.post('/voxi/agent', headers=self.headers,
                                              json={'action': 'settings_patch', 'settings': patch})
            self.assertEqual(response.status, 200)
        self.assertEqual(self.radio.agent.store.settings(),
                         {**original, 'enabled': True, 'instructions': '先给简洁结论。'})
        for patch in ({'unknown': True}, {'channels': []}):
            response = await self.client.post('/voxi/agent', headers=self.headers,
                                              json={'action': 'settings_patch', 'settings': patch})
            self.assertEqual(response.status, 400)
    async def test_voice_dispatch_saves_before_ack_without_sending_pcm_to_realtime(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True;self.radio.agent.store.save_settings(cfg)
        socket=SimpleNamespace(closed=False,send_json=AsyncMock())
        voice=VoiceSession(socket,'unused');voice.agent_radio=self.radio;voice.channel=1;voice.memory_user=self.uid;voice.memory_device='esp32-111111111111';voice.agent_request_id='voice-r';voice.say_agent=AsyncMock()
        self.radio.transcribe_memory=lambda pcm:'检查项目'
        await self.radio.agent_voice(voice,b'pcm')
        self.assertEqual(self.radio.agent.store.list_tasks(self.uid)[0]['prompt'],'检查项目')
        self.assertIn('任务已保存',voice.say_agent.call_args.args[0])
        self.assertEqual(socket.send_json.call_args.args[0]['type'],'task_state')
    async def test_native_ack_failure_closes_voice_without_losing_task(self):
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True;self.radio.agent.store.save_settings(cfg)
        socket=SimpleNamespace(closed=False,send_json=AsyncMock())
        async def close():socket.closed=True
        socket.close=close
        voice=VoiceSession(socket,'unused');voice.agent_radio=self.radio;voice.channel=1;voice.memory_user=self.uid;voice.memory_device='esp32-111111111111';voice.agent_request_id='r';voice.thread='voice'
        voice.rpc=SimpleNamespace(call=AsyncMock(side_effect=RuntimeError('speech transport failed')))
        self.radio.transcribe_memory=lambda pcm:'检查项目'
        await self.radio.agent_voice(voice,b'pcm')
        self.assertTrue(socket.closed)
        self.assertEqual(len(self.radio.agent.store.list_tasks(self.uid)),1)

    async def test_spoken_inventory_and_progress_use_current_channel_state_without_queueing(self):
        hub=self.radio.agent
        hub.store.save_settings({**DEFAULTS,'enabled':True})
        hub.store.save_settings([DEFAULTS['channels'][0],{'name':'代码频道','mode':'agent','skills':['code-review']}],self.uid)
        running,_=hub.store.submit(self.uid,'esp32-111111111111',1,'original','长任务')
        hub.store.update(running['id'],state='awaiting_approval',summary='等待远程调用确认')
        hub.store.submit(self.uid,'esp32-111111111111',1,'queued','误入队列的进度查询')
        socket=SimpleNamespace(closed=False,send_json=AsyncMock())
        voice=VoiceSession(socket,'unused')
        voice.agent_radio=self.radio;voice.channel=1;voice.memory_user=self.uid;voice.memory_device='esp32-111111111111'
        voice.say_agent=AsyncMock()
        for utterance in ('请问现在做到哪里了？','任务进展如何？','查看进度','要继续等吗'):
            self.radio.transcribe_memory=lambda _:utterance
            await self.radio.agent_voice(voice,b'fixture')
            self.assertIn('暂停在确认步骤',voice.say_agent.call_args.args[0])
            self.assertEqual(socket.send_json.call_args.args[0]['taskId'],running['id'])
        self.radio.transcribe_memory=lambda _:'Voxi Agent 有什么 Skill 或者 MCP？'
        await self.radio.agent_voice(voice,b'fixture')
        spoken=voice.say_agent.call_args.args[0]
        self.assertIn('代码审查',spoken)
        self.assertIn('MCP，尚未启用',spoken)
        self.assertNotIn('OpenAI 官方文档',spoken)
        self.assertEqual(len(hub.store.list_tasks(self.uid)),2)


if __name__=='__main__':unittest.main()
