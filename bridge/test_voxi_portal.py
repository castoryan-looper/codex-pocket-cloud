import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from voxi import VoxiRadio, VoiceSession
from voxi_portal import UserPortal
from voxi_agent_store import DEFAULTS


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

    async def test_user_endpoint_requires_internal_bearer_and_rejects_browser_origin(self):
        for headers in ({},{'Authorization':'Bearer device-user-token'},{'Authorization':'Bearer internal-only','Origin':'https://example.com'}):
            r=await self.client.post('/voxi/portal',json={'actorUserId':self.a,'action':'snapshot'},headers=headers);self.assertEqual(r.status,403)
