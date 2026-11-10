import asyncio
import copy
import json
import tempfile
import sqlite3
import unittest
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
from voxi_agent import AgentHub
from voxi_agent_store import DEFAULTS, AgentStore
from voxi_openclaw import endpoint, PublicResolver
from voxi import VoxiRadio
from test_voxi_agent import USER, OTHER, Rpc


async def until(fn):
    for _ in range(200):
        if fn():return
        await asyncio.sleep(.01)
    raise AssertionError('condition timed out')


class OpenClawTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.rpc_calls=[]
        def factory(notify):self.rpc_calls.append(True);return Rpc(notify)
        self.hub=AgentHub(self.temp.name,self.temp.name,factory)
        cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True;self.hub.store.save_settings(cfg)
        self.server=None;self.requests=[];self.events=[]

    async def asyncTearDown(self):
        await self.hub.close()
        if self.server:await self.server.close()
        self.temp.cleanup()

    def relay(self, user=USER):return self.hub.openclaw.create(user,'Home','relay')

    def route(self, ident, user=USER):
        channels=copy.deepcopy(DEFAULTS['channels']);channels[1]={'name':'My OpenClaw','mode':'openclaw','connectionId':ident}
        self.hub.openclaw.check_routes(channels,user);self.hub.store.save_settings(channels,user)

    async def poll(self, c, **extra):
        return await self.hub.openclaw.relay({'connectionId':c['connectionId'],'relayToken':c['pairingToken'],'status':'ready','op':'poll',**extra})

    async def test_muse_has_scoped_lease_results_and_continuation(self):
        connection=self.hub.openclaw.create_muse(USER,'Project')
        ident=connection['connectionId']
        channels=[DEFAULTS['channels'][0],{'name':'Muse Code','mode':'muse','connectionId':ident}]
        self.hub.check_routes(channels,USER)
        with self.assertRaises(ValueError):self.hub.check_routes(channels,OTHER)
        with self.assertRaises(ValueError):self.hub.check_routes([channels[0],{**channels[1],'mode':'openclaw'}],USER)
        with self.assertRaises(ValueError):await self.poll(connection)
        self.hub.store.save_settings(channels,USER)
        self.assertEqual(self.hub.openclaw.public(USER)[0]['engine'],'muse')
        await self.poll(connection,relayEngine='muse')
        self.assertTrue((await self.hub.openclaw.probe(USER,ident))['ok'])
        task,_=self.hub.submit(USER,None,1,'first-muse','Review files')
        await until(lambda:self.hub.store.task(task['id'])['state']=='submitting')
        job=(await self.poll(connection,relayEngine='muse'))['job']
        self.assertEqual(job['text'],'Review files')
        self.assertFalse(job['resume'])
        self.assertIsNone((await self.poll(connection,relayEngine='muse'))['job'])
        result={'relayEngine':'muse','op':'result','taskId':task['id'],'state':'completed','result':'Reviewed',
                'usage':{'inputTokens':5,'outputTokens':3,'totalTokens':8}}
        self.assertTrue((await self.poll(connection,**result))['ok'])
        self.assertTrue((await self.poll(connection,**result))['ok'])
        self.assertEqual(self.hub.store.user_usage(USER)['totalTokens'],8)
        follow=await self.hub.action(USER,task['id'],'continue',request_id='follow-muse',text='Keep going')
        await until(lambda:self.hub.store.task(follow['id'])['state']=='submitting')
        resumed=(await self.poll(connection,relayEngine='muse'))['job']
        self.assertEqual(resumed['session'],job['session'])
        self.assertTrue(resumed['resume'])
        await self.hub.action(USER,follow['id'],'cancel')
        stop=await self.poll(connection,relayEngine='muse',busy=True,taskId=follow['id'])
        self.assertEqual(stop['stop'],follow['id'])
        self.assertFalse(self.rpc_calls)

    async def fixture(self):
        async def models(request):
            self.requests.append((request.path,dict(request.headers),None))
            return web.json_response({'data':[{'id':'openclaw'}]})
        async def responses(request):
            self.requests.append((request.path,dict(request.headers),await request.json()))
            r=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await r.prepare(request)
            for event in self.events:await r.write(('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n').encode())
            await r.write(b'data: [DONE]\n\n');await r.write_eof();return r
        app=web.Application();app.router.add_get('/v1/models',models);app.router.add_post('/v1/responses',responses)
        self.server=TestServer(app);await self.server.start_server()
        c=self.hub.openclaw.create(USER,'Cloud','https','https://claw.example.com','writer','fixture-gateway-token')
        # Local HTTP is only a test fixture. Production rejects it before save.
        self.hub.store.db.execute('UPDATE openclaw_connections SET url=? WHERE id=?',(str(self.server.make_url('')).rstrip('/'),c['connectionId']))
        self.hub.openclaw.session=lambda seconds:aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds))
        self.route(c['connectionId']);return c

    async def test_direct_completion_uses_existing_agent_and_isolated_session(self):
        c=await self.fixture();self.events=[{'type':'response.output_text.delta','delta':'Done'},
          {'type':'response.completed','response':{'status':'completed','usage':{'input_tokens':5,'output_tokens':2,'total_tokens':7}}}]
        task,_=self.hub.submit(USER,None,1,'task','read only')
        await until(lambda:self.hub.store.task(task['id'])['state']=='completed')
        self.assertEqual(self.rpc_calls,[])
        _,headers,body=self.requests[0]
        self.assertEqual(headers['x-openclaw-agent-id'],'writer');self.assertTrue(headers['x-openclaw-session-key'].startswith('voxi-'+USER))
        self.assertEqual(body,{'model':'openclaw','input':'read only','stream':True})
        self.assertNotIn('fixture-gateway-token',json.dumps(self.hub.snapshot(None)))
        detail=self.hub.store.public_task(self.hub.store.task(task['id']),True);self.assertEqual(detail['usage']['total_tokens'],7)
        # Continue keeps original executor/session even after the channel is changed.
        self.hub.store.reset_channels(USER)
        continued=await self.hub.action(USER,task['id'],'continue',request_id='next',text='follow up')
        self.assertEqual(continued['engine'],'openclaw');self.assertEqual(continued['threadId'],task['threadId'])
        self.assertEqual(continued['connection_id'],c['connectionId'])

    async def test_stream_end_without_terminal_is_unknown_and_not_retried(self):
        await self.fixture();self.events=[{'type':'response.output_text.delta','delta':'I finished'}]
        task,_=self.hub.submit(USER,None,1,'same','do work')
        await until(lambda:self.hub.store.task(task['id'])['state']=='delivery_unknown')
        retry,created=self.hub.submit(USER,None,1,'same','do work');await asyncio.sleep(.1)
        self.assertFalse(created);self.assertEqual(retry['id'],task['id']);self.assertEqual(len(self.requests),1)

    async def test_incomplete_event_is_not_success(self):
        await self.fixture();self.events=[{'type':'response.incomplete','response':{'status':'incomplete'}}]
        task,_=self.hub.submit(USER,None,1,'short','do work')
        await until(lambda:self.hub.store.task(task['id'])['state']=='failed')

    async def test_probe_never_starts_agent_and_requires_owner(self):
        c=await self.fixture();result=await self.hub.openclaw.probe(USER,c['connectionId'])
        self.assertTrue(result['ok']);self.assertEqual(self.requests[0][0],'/v1/models')
        with self.assertRaises(ValueError):await self.hub.openclaw.probe(OTHER,c['connectionId'])

    async def test_lost_encryption_master_does_not_overwrite_openclaw_credentials(self):
        c=self.hub.openclaw.create(USER,'Cloud','https','https://claw.example.com','main','fixture-token')
        from pathlib import Path
        path=Path(self.temp.name)/'voxi-credentials.key';path.unlink()
        with self.assertRaises(ValueError):self.hub.openclaw.token(self.hub.openclaw.connection(c['connectionId']))
        self.assertFalse(path.exists())

    async def test_relay_scopes_claims_and_result_retries(self):
        c=self.relay();other=self.relay(OTHER);self.route(c['connectionId']);await self.poll(c)
        task,_=self.hub.submit(USER,None,1,'relay-task','work on home')
        await until(lambda:self.hub.store.task(task['id'])['state']=='submitting')
        self.assertIsNone((await self.poll(other))['job'])
        self.assertIsNone((await self.poll(c,busy=True))['job'])
        job=(await self.poll(c))['job'];self.assertEqual(job['id'],task['id']);self.assertEqual(job['text'],'work on home')
        self.assertIsNone((await self.poll(c))['job'])
        with self.assertRaises(ValueError):await self.hub.openclaw.relay({'connectionId':c['connectionId'],'relayToken':other['pairingToken'],'status':'ready','op':'poll'})
        result={'connectionId':c['connectionId'],'relayToken':c['pairingToken'],'status':'ready','op':'result','taskId':task['id'],'state':'completed','result':'Done at home'}
        self.assertTrue((await self.hub.openclaw.relay(result))['ok'])
        self.assertTrue((await self.hub.openclaw.relay(result))['ok'])
        self.assertEqual(self.hub.store.task(task['id'])['state'],'completed');self.assertEqual(self.rpc_calls,[])
        with self.assertRaises(ValueError):await self.hub.openclaw.relay({**result,'connectionId':other['connectionId'],'relayToken':other['pairingToken']})

    async def test_relay_cancel_does_not_claim_remote_stopped(self):
        c=self.relay();self.route(c['connectionId']);await self.poll(c);task,_=self.hub.submit(USER,None,1,'cancel','work')
        await until(lambda:self.hub.store.task(task['id'])['state']=='submitting');await self.poll(c)
        result=await self.hub.action(USER,task['id'],'cancel')
        self.assertEqual(result['state'],'delivery_unknown');self.assertIn('OpenClaw',result['summary'])
        self.assertIsNone((await self.poll(c))['job'])

    async def test_connection_and_channels_are_owned_and_inheritance_is_reversible(self):
        c=self.relay();self.route(c['connectionId'])
        with self.assertRaises(ValueError):self.route(c['connectionId'],OTHER)
        with self.assertRaises(ValueError):self.hub.openclaw.check_routes(self.hub.store.settings(USER)['channels'])
        with self.assertRaises(ValueError):self.hub.openclaw.delete(USER,c['connectionId'])
        self.hub.store.reset_channels(USER)
        self.assertEqual(self.hub.store.settings(USER)['channels'],DEFAULTS['channels'])
        self.assertEqual(self.hub.store.channel_overrides(),[])
        self.hub.openclaw.delete(USER,c['connectionId']);self.assertIsNone(self.hub.openclaw.connection(c['connectionId']))

    async def test_codex_disconnect_does_not_interrupt_openclaw(self):
        c=self.relay();self.route(c['connectionId']);await self.poll(c);task,_=self.hub.submit(USER,None,1,'disconnect','work')
        await until(lambda:self.hub.store.task(task['id'])['state']=='submitting');await self.poll(c)
        await self.hub.notification({'method':'pocket/disconnected'})
        self.assertEqual(self.hub.store.task(task['id'])['state'],'running')

    async def test_offline_connector_queues_without_holding_runtime_slots(self):
        c=self.relay();self.route(c['connectionId']);task,_=self.hub.submit(USER,None,1,'offline','work')
        await asyncio.sleep(.25)
        self.assertEqual(self.hub.store.task(task['id'])['state'],'queued');self.assertNotIn(task['id'],self.hub.active)
        await self.poll(c)
        await until(lambda:self.hub.store.task(task['id'])['state']=='submitting')
        result=await self.hub.action(USER,task['id'],'cancel')
        self.assertEqual(result['state'],'cancelled');self.assertIsNone((await self.poll(c))['job'])

    async def test_public_endpoint_policy_and_dns_rebinding(self):
        for url in ('http://claw.example.com','https://127.0.0.1:18789','https://169.254.169.254','https://user:pass@example.com','https://example.com/v1','https://example.com?token=x'):
            with self.assertRaises(ValueError,msg=url):endpoint(url)
        self.assertEqual(endpoint('https://claw.example.com/'),'https://claw.example.com')
        with self.assertRaises(OSError):await PublicResolver().resolve('localhost',443)


class OpenClawAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_old_task_database_migrates_without_dropping_history(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            db=sqlite3.connect(Path(directory)/'voxi-agent.sqlite3')
            db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,device_id TEXT,channel INTEGER NOT NULL,request_id TEXT NOT NULL,prompt TEXT NOT NULL,state TEXT NOT NULL,summary TEXT NOT NULL,result TEXT NOT NULL DEFAULT \'\',thread_id TEXT,turn_id TEXT,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(user_id,request_id))')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',('old',USER,None,1,'prior','prior task','completed','done','saved result','thread','turn',1,2))
            db.commit();db.close()
            store=AgentStore(directory)
            try:
                task=store.task('old');self.assertEqual(task['engine'],'codex');self.assertEqual(task['result'],'saved result')
                new,_=store.submit(USER,None,1,'new','new task');self.assertEqual(new['engine'],'codex')
            finally:store.close()

    async def test_admin_connect_bind_reset_and_relay_return_only_scoped_data(self):
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            radio=VoxiRadio(SimpleNamespace(runtime=Path(directory),config={'cwd':directory,'device_token':'internal'}),'unused')
            radio.memory.bind('esp32-111111111111');uid=radio.memory.owner('esp32-111111111111')
            app=web.Application();app.router.add_route('*','/voxi/agent',radio.agent_configuration)
            async with TestClient(TestServer(app)) as client:
                async def post(body):
                    response=await client.post('/voxi/agent',headers={'Authorization':'Bearer internal'},json=body)
                    self.assertEqual(response.status,200,await response.text());return await response.json()
                cfg=copy.deepcopy(DEFAULTS);cfg['enabled']=True;await post({'action':'settings','settings':cfg})
                c=await post({'action':'connect_openclaw','userId':uid,'name':'Home','transport':'relay'})
                self.assertIn('pairingToken',c)
                snap=await (await client.get('/voxi/agent',headers={'Authorization':'Bearer internal'})).json()
                self.assertNotIn(c['pairingToken'],json.dumps(snap))
                channels=copy.deepcopy(cfg['channels']);channels[1]={'name':'Home','mode':'openclaw','connectionId':c['connectionId']}
                await post({'action':'channels','userId':uid,'channels':channels})
                info=radio.channel_info(SimpleNamespace(memory_user=uid,channel=1))
                self.assertEqual(info['channelMode'],'agent');self.assertEqual(info['agentProvider'],'openclaw')
                snap=await post({'action':'reset_channels','userId':uid});self.assertNotIn(uid,snap['channelOverrides'])
                await post({'action':'delete_openclaw','userId':uid,'connectionId':c['connectionId']})
            await radio.agent.close()
