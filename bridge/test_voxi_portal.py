import copy
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from voxi import VoxiRadio, VoiceSession
from voxi_portal import UserPortal
from voxi_agent_store import DEFAULTS
from voxi_models import CHAT_ENDPOINTS, DIFY_API, dify_inputs, dify_result, model_request, model_result


class ProviderContractTests(unittest.TestCase):
    def test_model_protocols_use_fixed_hosts_and_provider_specific_payloads(self):
        for provider in (*CHAT_ENDPOINTS, 'openai', 'anthropic', 'qwen'):
            with self.subTest(provider=provider):
                url, headers, body = model_request(
                    {'provider': provider, 'model': 'test/model', 'workspace': 'ws-fixture'}, 'fixture', 'Hello', 100)
                self.assertTrue(url.startswith('https://'))
                self.assertEqual(body['model'], 'test/model')
                if provider == 'openai':
                    self.assertTrue(url.endswith('/responses'))
                    self.assertFalse(body['store'])
                    self.assertEqual(body['max_output_tokens'], 100)
                elif provider == 'anthropic':
                    self.assertTrue(url.endswith('/messages'))
                    self.assertEqual(headers['x-api-key'], 'fixture')
                    self.assertNotIn('Authorization', headers)
                else:
                    self.assertTrue(url.endswith('/chat/completions'))
                    self.assertFalse(body['stream'])
                    self.assertEqual(body['messages'], [{'role': 'user', 'content': 'Hello'}])
                    self.assertEqual(body['max_completion_tokens' if provider == 'minimax' else 'max_tokens'], 100)

    def test_native_responses_extract_text_and_usage_only_from_completed_output(self):
        value = {'status': 'completed', 'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'Hello'}]}],
            'usage': {'input_tokens': 6, 'output_tokens': 2, 'total_tokens': 8}}
        self.assertEqual(model_result('openai', value), ('Hello', {'inputTokens': 6, 'outputTokens': 2, 'totalTokens': 8}))
        with self.assertRaises(ValueError):
            model_result('openai', {**value, 'status': 'incomplete'})
        value = {'stop_reason': 'end_turn', 'content': [{'type': 'thinking'}, {'type': 'text', 'text': 'Hello'}],
                 'usage': {'input_tokens': 1, 'cache_read_input_tokens': 5, 'output_tokens': 2}}
        self.assertEqual(model_result('anthropic', value), ('Hello', {'inputTokens': 6, 'outputTokens': 2, 'totalTokens': 8}))


class DifyContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_requires_text_input_and_successful_terminal_with_configured_output(self):
        parameters = {'user_input_form': [
            {'paragraph': {'variable': 'query', 'required': True}},
            {'text-input': {'variable': 'language', 'required': True, 'default': 'zh'}},
        ]}
        self.assertEqual(dify_inputs(parameters, 'query'), {'language': 'zh'})
        with self.assertRaises(ValueError):
            dify_inputs(parameters, 'missing')

        async def events(*payloads):
            for payload in payloads:
                yield b'data: ' + json.dumps(payload).encode() + b'\n'

        node = {'event': 'node_finished', 'data': {'outputs': {'result': 'not final'}}}
        end = {'event': 'workflow_finished', 'data': {'status': 'succeeded', 'outputs': {'result': 'Done'},
                                                     'total_tokens': 11}}
        response = SimpleNamespace(status=200, content=events(node, end))
        self.assertEqual(await dify_result(response, 'result'), ('Done', {'totalTokens': 11}, None))
        response = SimpleNamespace(status=200, content=events(node))
        with self.assertRaisesRegex(ValueError, '提前关闭'):
            await dify_result(response, 'result')


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.radio=VoxiRadio(SimpleNamespace(runtime=Path(self.tmp.name),config={'device_token':'internal-only','cwd':self.tmp.name}),'unused')
        self.a=self.radio.memory.bind('esp32-111111111111');self.b=self.radio.memory.bind('esp32-222222222222')
        self.radio.memory.change({'action':'profile','userId':self.b,'name':'Bob private name','profile':'private background','enabled':True})
        self.radio.memory.record(self.b,'esp32-222222222222','bob-voice','我喜欢秘密资料')
        self.conn=self.radio.agent.openclaw.create(self.b,'Bob private connection','relay')
        self.task,_=self.radio.agent.store.submit(self.b,None,1,'bob-task','Bob private task')
        self.radio.agent.store.save_secret('openai','fixture-secret-never-api')
        self.api=UserPortal(self.radio)
        app=web.Application();app.router.add_post('/voxi/portal',self.api.handle)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();await self.radio.agent.close();self.tmp.cleanup()
    async def call(self,body,user=None):
        r=await self.client.post('/voxi/portal',json={'actorUserId':user or self.a,**body},headers={'Authorization':'Bearer internal-only'})
        return r,await r.json()

    async def test_dify_validation_is_read_only_and_completed_chat_continues_for_same_owner(self):
        hub = self.radio.agent
        hub.store.save_settings({**hub.store.settings(), 'enabled': True})
        posted = []

        async def events(body):
            posted.append(body)
            for event in ({'event': 'message', 'answer': '你好'},
                          {'event': 'workflow_finished', 'data': {'status': 'succeeded'}},
                          {'event': 'message_end', 'conversation_id': 'dify-thread', 'metadata': {'usage': {'total_tokens': 9}}}):
                yield b'data: ' + json.dumps(event).encode() + b'\n'

        @asynccontextmanager
        async def post(url, *, headers, json, allow_redirects):
            self.assertEqual(url, DIFY_API + '/chat-messages')
            self.assertEqual(headers, {'Authorization': 'Bearer fixture-app-key'})
            self.assertFalse(allow_redirects)
            yield SimpleNamespace(status=200, content=events(json))

        fixture = SimpleNamespace(post=post)
        @asynccontextmanager
        async def client(_):
            yield fixture

        async def metadata(client, method, url, headers):
            self.assertEqual(method, 'GET')
            self.assertEqual(headers, {'Authorization': 'Bearer fixture-app-key'})
            return {'mode': 'advanced-chat'} if url.endswith('/info') else {'user_input_form': []}

        with patch.object(hub.models, 'client', client), patch('voxi_models.request_json', metadata), patch.object(hub, 'start'):
            response, created = await self.call({'action':'connect_model', 'provider':'dify-chat', 'name':'研究助手', 'key':'fixture-app-key'})
            self.assertEqual(response.status, 200)
            self.assertEqual(posted, [])
            ident = created['connectionId']
            response, _ = await self.call({'action':'channels', 'channels':[
                {'slot':1, 'name':'研究助手', 'mode':'model', 'connectionId':ident}]})
            self.assertEqual(response.status, 200)
            _, first = await self.call({'action':'submit', 'channel':1, 'requestId':'dify-one', 'text':'你好'})
            await hub.run(first['task']['id'])
            _, details = await self.call({'action':'detail', 'taskId':first['task']['id']})
            self.assertEqual(details['task']['result'], '你好')
            self.assertEqual(details['task']['usage']['total_tokens'], 9)
            self.assertEqual(details['task']['threadId'], 'dify-thread')
            body = {'action':'continue', 'taskId':first['task']['id'], 'requestId':'dify-two', 'text':'请继续'}
            response, _ = await self.call(body, self.b)
            self.assertEqual(response.status, 400)
            response, second = await self.call(body)
            self.assertEqual(response.status, 200)
            await hub.run(second['task']['id'])
            self.assertNotIn('conversation_id', posted[0])
            self.assertEqual(posted[1]['conversation_id'], 'dify-thread')
            self.assertEqual(posted[0]['user'], posted[1]['user'])

    async def test_user_snapshot_contains_only_owner_data_and_no_system_configuration(self):
        r,data=await self.call({'action':'snapshot'});self.assertEqual(r.status,200)
        raw=json.dumps(data,ensure_ascii=False)
        for text in (self.b,'Bob private','private background','秘密资料','fixture-secret-never-api','credentials','instructions','maxConcurrent'):
            self.assertNotIn(text,raw)
        self.assertEqual(data['role'],'user');self.assertEqual(data['profile']['id'],self.a)
        self.assertEqual([d['id'] for d in data['devices']],['esp32-111111111111'])

    async def test_cross_owner_and_administrator_actions_are_rejected(self):
        for body in ({'action':'snapshot','userId':self.b}, {'action':'detail','taskId':self.task['id']},
                     {'action':'cancel','taskId':self.task['id']}, {'action':'test_openclaw','connectionId':self.conn['connectionId']},
                     {'action':'delete_openclaw','connectionId':self.conn['connectionId']},
                     {'action':'rename_device','deviceId':'esp32-222222222222','name':'stolen'},
                     {'action':'rebind','deviceId':'esp32-222222222222'},
                     {'action':'settings','settings':DEFAULTS}, {'action':'credential','provider':'openai','key':''},
                     {'action':'create_profile','name':'forbidden'}, {'action':'relay'}):
            r,_=await self.call(body);self.assertEqual(r.status,400,body)
        channels=copy.deepcopy(DEFAULTS['channels']);channels[1]={'name':'foreign','mode':'openclaw','connectionId':self.conn['connectionId']}
        r,_=await self.call({'action':'channels','channels':channels});self.assertEqual(r.status,400)
        self.assertEqual(self.radio.memory.owner('esp32-222222222222'),self.b)
        self.assertEqual(self.radio.agent.store.task(self.task['id'])['state'],'queued')

    async def test_profile_preferences_and_channels_save_while_service_is_off(self):
        r,data=await self.call({'action':'profile','name':'Alice','profile':'Designer','enabled':True});self.assertEqual(r.status,200)
        self.assertEqual(data['profile']['name'],'Alice')
        r,_=await self.call({'action':'preferences','preferences':{'fastPrompt':'先给结论'}});self.assertEqual(r.status,200)
        self.assertEqual(self.radio.agent.store.preferences(self.a)['fastPrompt'],'先给结论')
        self.assertEqual(self.radio.agent.store.preferences(self.b)['fastPrompt'],'')
        channels=copy.deepcopy(DEFAULTS['channels']);channels[0]['name']='我的快问'
        r,data=await self.call({'action':'channels','channels':channels});self.assertEqual(r.status,200)
        self.assertFalse(data['serviceEnabled']);self.assertFalse(data['inheritsChannels'])
        self.assertEqual(self.radio.agent.store.settings(self.b)['channels'][0]['name'],'快问')
        r,data=await self.call({'action':'reset_channels'});self.assertEqual(r.status,200);self.assertTrue(data['inheritsChannels'])

    async def test_connection_creation_is_pinned_to_authenticated_owner(self):
        r,c=await self.call({'action':'connect_openclaw','name':'Alice home','transport':'relay'});self.assertEqual(r.status,200)
        self.assertEqual(self.radio.agent.openclaw.connection(c['connectionId'])['user_id'],self.a)
        _,data=await self.call({'action':'snapshot'});self.assertEqual(len(data['connections']),1)
        self.assertNotIn(c['pairingToken'],json.dumps(data))

    async def test_verified_model_is_owned_and_executes_from_a_sparse_channel(self):
        hub=self.radio.agent
        secret='key-private-model-test'
        model={'action':'connect_model','name':'摘要','provider':'deepseek','model':'deepseek-flash','key':secret}
        with patch.object(hub.models,'complete',new_callable=AsyncMock) as complete:
            complete.side_effect=ValueError('请检查 API Key')
            response,_=await self.call(model)
            self.assertEqual(response.status,400)
            self.assertEqual(hub.models.public(self.a),[])
            complete.side_effect=None
            complete.return_value=('模型回复',{'totalTokens':17})
            response,created=await self.call(model)
            self.assertEqual(response.status,200)
            ident=created['connectionId']
            self.assertNotIn(secret.encode(),hub.models.connection(self.a,ident)['secret'])
            channels=[{'slot':0,'name':'快问','mode':'fast'},{'slot':12,'name':'摘要','mode':'model','connectionId':ident}]
            response,_=await self.call({'action':'channels','channels':channels},self.b)
            self.assertEqual(response.status,400)
            response,snapshot=await self.call({'action':'channels','channels':channels})
            self.assertEqual(response.status,200)
            self.assertEqual(snapshot['channels'],channels)
            self.assertNotIn(secret,json.dumps(snapshot))
            response,_=await self.call({'action':'delete_model','connectionId':ident})
            self.assertEqual(response.status,400)
            hub.store.save_settings({**hub.store.settings(),'enabled':True})
            session=VoiceSession(SimpleNamespace(closed=False,send_str=AsyncMock()),'unused')
            session.agent_radio=self.radio
            session.memory_user=self.a
            await session.send_channel_catalog()
            self.assertEqual(len(json.loads(session.ws.send_str.call_args.args[0])['channels']),4)
            session.channel_limit=16
            session.ws.send_str.reset_mock()
            await session.send_channel_catalog()
            blocks=[json.loads(call.args[0]) for call in session.ws.send_str.call_args_list]
            self.assertEqual([block['offset'] for block in blocks],[0,4,8,12])
            self.assertEqual(blocks[-1]['channels'][0][:2],['摘要','agent'])
            self.assertTrue(all(len(call.args[0].encode())<=2048 for call in session.ws.send_str.call_args_list))
            with patch.object(hub,'start'):
                response,created=await self.call({'action':'submit','channel':12,'requestId':'model-job','text':'请总结这句话'})
            self.assertEqual(response.status,200)
            task=created['task']
            await hub.run(task['id'])
            complete.assert_awaited_with({'id':ident,'user_id':self.a,'name':'摘要','provider':'deepseek',
                                          'model':'deepseek-flash','workspace':'','secret':hub.models.connection(self.a,ident)['secret']},
                                         secret,'请总结这句话')
            _,details=await self.call({'action':'detail','taskId':task['id']})
            self.assertEqual(details['task']['state'],'completed')
            self.assertEqual(details['task']['result'],'模型回复')
            self.assertEqual(details['task']['usage']['total_tokens'],17)
            response,snapshot=await self.call({'action':'channels','channels':channels[1:]})
            self.assertEqual(response.status,200)
            self.assertEqual(snapshot['channels'][0]['slot'],12)

    async def test_new_profile_does_not_claim_or_move_existing_devices(self):
        user=self.radio.memory.create_user('New user')
        self.assertEqual(self.radio.memory.owned_snapshot(user['id'])['devices'],[])
        self.assertEqual(self.radio.memory.owner('esp32-111111111111'),self.a)
        self.assertEqual(self.radio.memory.owner('esp32-222222222222'),self.b)

    async def test_context_pairing_is_scoped_and_removed_chats_do_not_resync(self):
        response, secret = await self.call({'action':'context_authorize','source':'codex','name':'家庭电脑'})
        self.assertEqual(response.status,200)
        payload={'id':'local-chat','title':'Voxi modem',
                 'updated':int(time.time()*1000),'messages':[{'role':'user','text':'检查 Voxi modem。Bearer hidden-test-token-12345'}]}
        async def push(enabled):
            response=await self.client.post('/voxi/portal',json={'action':'context_ingest','token':secret['token'],
                'enabledUsers':enabled,'chat':payload},headers={'Authorization':'Bearer internal-only'})
            return response,await response.json()
        response,_=await push([self.b])
        self.assertEqual(response.status,400)
        response,_=await self.call({'action':'context_ingest','token':secret['token'],'enabledUsers':[self.a],'chat':payload})
        self.assertEqual(response.status,400)
        response,result=await push([self.a])
        self.assertEqual(response.status,200)
        self.assertTrue(result['changed'])
        _,result=await push([self.a])
        self.assertFalse(result['changed'])
        _,owner=await self.call({'action':'context_list'})
        _,foreign=await self.call({'action':'context_list'},self.b)
        self.assertEqual(foreign['chats'],[])
        self.assertNotIn(secret['token'],json.dumps(owner))
        ident=owner['chats'][0]['id']
        response,_=await self.call({'action':'context_detail','id':ident},self.b)
        self.assertEqual(response.status,400)
        _,details=await self.call({'action':'context_detail','id':ident})
        self.assertNotIn('hidden-test-token',json.dumps(details))
        await self.call({'action':'context_pin','id':ident})
        context=self.radio.agent.context
        self.assertIn('Voxi modem',context.reference(self.a,'继续上次的任务'))
        self.assertEqual(context.reference(self.b,'继续上次的任务'),'')
        await self.call({'action':'context_remove','id':ident})
        _,result=await push([self.a])
        self.assertFalse(result['changed'])
        await self.call({'action':'context_revoke','id':owner['connectors'][0]['id']})
        response,_=await push([self.a])
        self.assertEqual(response.status,400)

    async def test_channel_voice_priority_and_per_user_reported_usage(self):
        store=self.radio.agent.store
        store.save_settings({**store.settings(),'enabled':True})
        store.save_preferences(self.a,{'fastPrompt':'','voice':'ember'})
        response,_=await self.call({'action':'channels','channels':[
            {'slot':12,'name':'播报','mode':'agent','voice':'maple'},
            {'slot':15,'name':'普通','mode':'fast'}]})
        self.assertEqual(response.status,200)
        voice=VoiceSession(SimpleNamespace(closed=False),'unused')
        voice.agent_radio=self.radio;voice.memory_user=self.a
        self.assertEqual(voice.selected_voice({'voice':'cove'},12),'maple')
        self.assertEqual(voice.selected_voice({'voice':'cove'},15),'ember')
        voice.voice_override='spruce'
        self.assertEqual(voice.selected_voice({'voice':'cove'},12),'spruce')
        first,_=store.submit(self.a,None,12,'counted','One')
        store.submit(self.a,None,12,'unmetered','Two')
        store.set_usage(first['id'],{'totalTokens':31})
        store.set_usage(self.task['id'],{'totalTokens':800})
        for _ in range(2):
            store.record_voice_usage(self.a,'response-1','fast',{'total_tokens':19})
        _,snapshot=await self.call({'action':'snapshot'})
        self.assertEqual(snapshot['usage']['totalTokens'],50)
        self.assertEqual(snapshot['usage']['measuredTasks'],1)
        self.assertEqual(snapshot['usage']['tasks'],2)
        self.assertEqual(snapshot['usage']['voice'][0]['responses'],1)

    async def test_user_endpoint_requires_internal_bearer_and_rejects_browser_origin(self):
        for headers in ({},{'Authorization':'Bearer device-user-token'},{'Authorization':'Bearer internal-only','Origin':'https://example.com'}):
            r=await self.client.post('/voxi/portal',json={'actorUserId':self.a,'action':'snapshot'},headers=headers);self.assertEqual(r.status,403)
