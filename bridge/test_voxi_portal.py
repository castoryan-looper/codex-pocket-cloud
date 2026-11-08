import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from voxi import VoxiRadio
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

    async def test_new_profile_does_not_claim_or_move_existing_devices(self):
        user=self.radio.memory.create_user('New user')
        self.assertEqual(self.radio.memory.owned_snapshot(user['id'])['devices'],[])
        self.assertEqual(self.radio.memory.owner('esp32-111111111111'),self.a)
        self.assertEqual(self.radio.memory.owner('esp32-222222222222'),self.b)

    async def test_user_endpoint_requires_internal_bearer_and_rejects_browser_origin(self):
        for headers in ({},{'Authorization':'Bearer device-user-token'},{'Authorization':'Bearer internal-only','Origin':'https://example.com'}):
            r=await self.client.post('/voxi/portal',json={'actorUserId':self.a,'action':'snapshot'},headers=headers);self.assertEqual(r.status,403)
