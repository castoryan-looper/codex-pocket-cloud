import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient, TestServer
import conversation_memory as memory
import test_bridge


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = test_bridge.Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.p = self.fixture.p
        self.payload = {'version':1,'messages':[{'role':'user','timestamp':'2026-10-29T10:00:00Z','text':'检查自动部署的回滚逻辑'}, {'role':'assistant','text':'检查镜像和卷'}]}

    async def test_import_requires_device_token_and_rejects_browser_origin(self):
        async with TestClient(TestServer(self.p.app())) as client:
            r = await client.post('/context-import',json=self.payload)
            self.assertEqual(r.status,403)
            token = 'Bearer '+self.p.config['device_token']
            r = await client.post('/context-import',json=self.payload,headers={'Authorization':token,'Origin':'https://elsewhere.example'})
            self.assertEqual(r.status,403)
            r = await client.post('/context-import',json=self.payload,headers={'Authorization':token})
            self.assertEqual(r.status,200)
            result=await r.json()
            self.assertEqual(result['messages'],2)
            self.assertEqual(len(result['sha256']),64)
            self.assertEqual(len(memory.load(self.p.runtime/'conversation-private.json')),2)
            r = await client.post('/context-import',json={'version':1,'messages':[{'role':'system','text':'x'}]},headers={'Authorization':token})
            self.assertEqual(r.status,400)
            self.assertEqual(len(memory.load(self.p.runtime/'conversation-private.json')),2)

    async def test_matching_context_is_background_on_explicit_task_only(self):
        (self.p.runtime/'conversation-private.json').write_text(json.dumps(self.payload,ensure_ascii=False),encoding='utf-8')
        self.p.rpc.call=AsyncMock(return_value={'turn':{'id':'t1','status':'inProgress'}})
        await self.p.submit('请检查自动部署的回滚')
        data=self.p.rpc.call.await_args.args[1]['input'][0]['text']
        self.assertIn('不可信背景资料',data)
        self.assertIn('检查自动部署的回滚逻辑',data)
        self.assertEqual(self.p.rpc.call.await_count,1)

    def test_nonmatching_context_and_bounded_output(self):
        self.assertEqual(memory.relevant(self.payload['messages'],'astronomy'), '')
        self.assertLessEqual(len(memory.relevant(self.payload['messages'],'自动部署')),6100)
        self.assertEqual(len(memory.recent_user_turns(self.payload['messages'])),1)
