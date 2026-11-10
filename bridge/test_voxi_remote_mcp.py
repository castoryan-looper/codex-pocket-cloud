import asyncio
from contextlib import asynccontextmanager
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_voxi_agent import Rpc, USER, OTHER
from test_voxi_openclaw import until
from voxi_agent import AgentHub
from voxi_agent_store import DEFAULTS
from voxi_remote_mcp import connection_parameters, descriptors, public_ip


class RemoteMcpTests(unittest.IsolatedAsyncioTestCase):
    def test_registry_config_restricts_target_and_protects_keys(self):
        manifest={'remotes':[
            {'type':'streamable-http','url':'https://{tenant}.example.com/mcp','variables':{'tenant':{}}},
            {'type':'streamable-http','url':'https://127.0.0.1/mcp'},
            {'type':'streamable-http','url':'https://api.example.com:444/mcp'},
            {'type':'streamable-http','url':'https://api.example.com/mcp','headers':[{'name':'Host'}]},
            {'type':'streamable-http','url':'https://api.example.com/mcp/{tenant}','variables':{'tenant':{'isRequired':True}},
             'headers':[{'name':'X-API-Key','isRequired':True}]},
        ]}
        entries=descriptors(manifest)
        self.assertEqual([entry['index'] for entry in entries],[4])
        self.assertTrue(entries[0]['fields'][1]['secret'])
        values={'variable:tenant':'team/a','header:X-API-Key':'fixture'}
        self.assertEqual(connection_parameters(entries[0],values),('https://api.example.com/mcp/team%2Fa',{'X-API-Key':'fixture'}))
        with self.assertRaises(ValueError):
            connection_parameters(entries[0],{**values,'header:X-API-Key':'a\r\nb'})
        self.assertFalse(public_ip('127.0.0.1'))
        self.assertFalse(public_ip('::ffff:169.254.169.254'))

    async def test_verified_connection_is_scoped_to_owner_and_each_tool_approval(self):
        with tempfile.TemporaryDirectory() as root:
            rpcs=[]
            def factory(notify):
                rpc=Rpc(notify);rpcs.append(rpc);return rpc
            hub=AgentHub(root,root,factory)
            try:
                await self.exercise(hub,rpcs)
            finally:
                await hub.close()

    async def exercise(self,hub,rpcs):
        lib=hub.capabilities.remote
        key='fixture-only-key'
        send=AsyncMock(return_value={'content':[{'type':'text','text':'fixture response'}]})
        tools=[{'name':name,'inputSchema':{'type':'object'}} for name in ('lookup','remove')]
        conn=type('Connection',(),{})()
        conn.send=send;conn.tools=AsyncMock(return_value=tools)
        @asynccontextmanager
        async def upstream(url,headers,seconds):
            self.assertEqual(headers,{'X-API-Key':key})
            yield conn
        entry=descriptors({'remotes':[{'type':'streamable-http','url':'https://api.example.com/mcp',
                                     'headers':[{'name':'X-API-Key','isRequired':True}]}]})
        with patch.object(lib,'entry',AsyncMock(return_value=entry)),patch('voxi_remote_mcp.upstream',upstream):
            connect={'action':'capability_remote_connect','server':'example.test/mcp','version':'1','index':0,
                     'values':{'header:X-API-Key':key}}
            ident=(await hub.capabilities.manage(USER,connect))['id']
            self.assertNotIn(key,json.dumps(lib.list(USER)))
            self.assertNotIn(key.encode(),lib.get(USER,ident)['secret'])
            self.assertEqual(lib.list(OTHER),[])
            channels=[DEFAULTS['channels'][0],{'name':'Remote','mode':'agent','mcp':[ident]}]
            with self.assertRaises(ValueError):hub.check_routes(channels,USER)
            with self.assertRaises(ValueError):hub.check_routes(channels,OTHER)
            await hub.capabilities.manage(USER,{'action':'capability_remote_test','id':ident,
                'selected':['lookup'],'tool':'lookup','arguments':{'q':'probe'}})
            hub.check_routes(channels,USER)
            with self.assertRaises(ValueError):hub.check_routes(channels,OTHER)
            hub.store.save_settings({**DEFAULTS,'enabled':True})
            hub.store.save_settings(channels,USER)
            task,_=hub.submit(USER,'esp32-111111111111',1,'remote-fixture','look up fixture')
            await until(lambda:hub.store.task(task['id'])['state']=='running')
            config=next(p for method,p in rpcs[0].calls if method=='thread/start')['config']['mcp_servers'][ident]
            self.assertNotIn(key,json.dumps(config))
            app=web.Application();app.router.add_post('/mcp',lib.mcp)
            async with TestClient(TestServer(app)) as client:
                async def call(tool):
                    r=await client.post('/mcp',headers=config['http_headers'],
                         json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':tool,'arguments':{'q':'test'}}})
                    return (await r.json())['result']
                self.assertTrue((await call('remove'))['isError'])
                pending=asyncio.create_task(call('lookup'))
                await until(lambda:hub.store.task(task['id'])['state']=='awaiting_approval')
                approval=hub.store.public_task(hub.store.task(task['id']),True)['approval']
                self.assertEqual(send.await_count,1)
                for user,approval_id in ((OTHER,approval['id']),(USER,None),(USER,'wrong')):
                    with self.assertRaises(ValueError):
                        await hub.action(user,task['id'],'approve',approval_id=approval_id)
                await hub.action(USER,task['id'],'approve',approval_id=approval['id'])
                self.assertEqual((await pending)['content'][0]['text'],'fixture response')
                self.assertEqual(send.await_count,2)
                with self.assertRaises(ValueError):
                    await hub.action(USER,task['id'],'approve',approval_id=approval['id'])
            await hub.capabilities.manage(USER,{'action':'capability_remote_remove','id':ident})
            self.assertEqual(lib.list(USER),[])
            self.assertFalse(hub.store.settings(USER)['channels'][1].get('mcp'))
