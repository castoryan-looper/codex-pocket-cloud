import asyncio
import tempfile
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from voxi import VoxiRadio
from voxi_agent_store import AgentStore
from voxi_memory import MemoryStore, device_id

A='esp32-111111111111'
B='esp32-222222222222'

class MemoryTests(unittest.TestCase):
    def test_registered_devices_can_be_retired_and_explicitly_registered_again(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(directory);a=store.create_user('A')['id'];b=store.bind(B)
            key='a'*64;new_key='b'*64
            self.assertEqual(store.admit_device(B,'legacy'),b)
            with self.assertRaisesRegex(ValueError,'未登记'):store.admit_device(A,'legacy')
            store.register_device(a,A,'Test radio',key)
            self.assertEqual(store.bind(A,registered_owner=a,proof=key),a)
            with self.assertRaises(ValueError):store.bind(A,registered_owner=b,proof=key)
            with self.assertRaises(ValueError):store.bind(A)
            with self.assertRaises(ValueError):store.admit_device(A,'legacy')
            with self.assertRaises(ValueError):store.register_device(b,A,'',new_key)
            with self.assertRaisesRegex(ValueError,'绑定设备'):store.delete_user(a,retire_devices=[B])
            self.assertEqual(store.owner(A),a);self.assertEqual(store.owner(B),b)
            store.delete_user(a,retire_devices=[A]);store.delete_user(a,retire_devices=[A])
            reopened=MemoryStore(directory)
            self.assertEqual([d['id'] for d in reopened.registry()['devices']],[B])
            with self.assertRaisesRegex(ValueError,'退役'):reopened.bind(A)
            with self.assertRaises(ValueError):reopened.admit_device(A,key)
            reopened.register_device(b,A,'',new_key)
            with self.assertRaises(ValueError):reopened.admit_device(A,key)
            self.assertEqual(reopened.admit_device(A,new_key),b)
            self.assertNotIn('key_hash',reopened.user_devices(b)[0])

    def test_voice_counts_survive_restart_and_today_uses_beijing_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(directory)
            beijing_midnight=86400*20000-8*3600
            with patch('voxi_memory.time.time',return_value=beijing_midnight-1):
                store.record_voice(1200)
            with patch('voxi_memory.time.time',return_value=beijing_midnight+1):
                reopened=MemoryStore(directory)
                self.assertEqual(reopened.product_metrics()['todayTurns'],0)
                reopened.record_voice(3000)
                stats=reopened.product_metrics()
            self.assertEqual((stats['voiceTurns'],stats['voiceInputMs'],stats['todayTurns'],stats['todayInputMs']),(2,4200,1,3000))

    def test_device_name_migration_preserves_identity_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'voxi-memory.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE devices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,last_seen REAL NOT NULL)')
                db.execute('INSERT INTO devices VALUES(?,?,?)',(A,'old-user',1))
            db.close()
            store=MemoryStore(directory)
            # Existing binding survives migration; independent display name is new.
            with sqlite3.connect(path) as db:db.execute("INSERT INTO users(id,name) VALUES('old-user','原用户')")
            db.close()
            uid=store.bind(A);store.record(uid,A,'source','我喜欢天文学')
            store.change({'action':'rename_device','userId':uid,'deviceId':A,'name':'我的床头设备'})
            restarted=MemoryStore(directory)
            self.assertEqual(restarted.owner(A),'old-user');self.assertIn('天文学',restarted.context(uid))
            self.assertEqual(restarted.registry()['devices'][0]['name'],'我的床头设备')
            self.assertEqual(restarted.registry()['devices'][0]['id'],A)
            other=store.bind(B)
            with self.assertRaises(ValueError):store.change({'action':'rename_device','userId':other,'deviceId':A,'name':'错误'})
            with self.assertRaises(ValueError):store.change({'action':'rename_device','userId':uid,'deviceId':A,'name':''})

    def test_isolation_persistence_and_explicit_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            store=MemoryStore(directory);a=store.bind(A);b=store.bind(B)
            self.assertEqual(store.bind(A),a);self.assertNotEqual(a,b)
            store.record(a,A,'one','我喜欢短一点的故事。我今天去了公园。')
            store.record(b,B,'two','我喜欢详细讲解。')
            self.assertIn('短一点',store.context(a));self.assertNotIn('详细讲解',store.context(a))
            self.assertEqual(len(store.snapshot()['memories']),2)
            store.record(a,A,'one','我喜欢短一点的故事。');self.assertEqual(len(store.snapshot()['turns']),2)
            self.assertEqual(MemoryStore(directory).context(a),store.context(a))
            store.record(a,B,'spoof','我喜欢错误的数据');self.assertNotIn('错误的数据',store.context(a))
            self.assertIsNone(store.bind(None));self.assertIsNone(device_id('../../escape'))

    def test_edit_disable_delete_clear_and_binding(self):
        store=MemoryStore();a=store.bind(A);b=store.bind(B)
        store.record(a,A,'one','我喜欢故事。')
        mid=store.snapshot()['memories'][0]['id']
        store.change({'action':'memory','userId':a,'id':mid,'text':'我更喜欢科普'})
        self.assertIn('科普',store.context(a))
        store.change({'action':'profile','userId':a,'name':'Castor','profile':'喜欢自然','enabled':False})
        store.record(a,A,'two','我喜欢散步');self.assertEqual(store.context(a),'');self.assertEqual(len(store.snapshot()['turns']),1)
        store.change({'action':'profile','userId':a,'name':'Castor','profile':'喜欢自然','enabled':True})
        store.change({'action':'clear','userId':a});self.assertEqual(store.snapshot()['turns'],[]);self.assertEqual(store.snapshot()['memories'],[])
        store.change({'action':'rebind','userId':b,'deviceId':A});self.assertEqual(store.owner(A),b)
        store.record(a,A,'three','我喜欢错误');self.assertNotIn('错误',store.context(a))
        with self.assertRaises(ValueError):store.change({'action':'profile','userId':a,'name':'','profile':'','enabled':True})

    def test_delete_source_removes_derived_fact_and_retention_is_bounded(self):
        store=MemoryStore();a=store.bind(A)
        store.record(a,A,'one','我喜欢音乐');store.change({'action':'delete_turn','userId':a,'id':'one'})
        self.assertEqual(store.snapshot()['memories'],[])
        for i in range(220):store.record(a,A,str(i),'我喜欢第'+str(i)+'种故事')
        self.assertEqual(len(store.snapshot()['turns']),200);self.assertEqual(len(store.snapshot()['memories']),100)

class MemoryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_requires_auth_closes_only_changed_device_and_rejects_stale_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            radio=VoxiRadio(SimpleNamespace(config={'device_token':'private'},runtime=Path(directory)),'unused')
            a=radio.memory.create_user('A')['id'];b=radio.memory.create_user('B')['id']
            radio.memory.register_device(a,A,'Radio','a'*64)
            class Socket:
                prepared=True
                closed=False
                async def close(self,**kwargs):self.closed=True
            changed=Socket();untouched=Socket();radio.active=[SimpleNamespace(memory_device=A,ws=changed),SimpleNamespace(memory_device=B,ws=untouched)]
            app=web.Application();app.router.add_post('/voxi/memory',radio.memory_configuration)
            async with TestClient(TestServer(app)) as client:
                body={'action':'transfer_device','userId':b,'sourceUserId':a,'deviceId':A,'confirmDeviceId':A,'keyHash':'b'*64};headers={'Authorization':'Bearer private'}
                self.assertEqual((await client.post('/voxi/memory',json=body)).status,403)
                self.assertEqual((await client.post('/voxi/memory',headers=headers,json=body)).status,200)
                self.assertTrue(changed.closed);self.assertFalse(untouched.closed)
                self.assertEqual(radio.memory.admit_device(A,'b'*64),b)
                self.assertEqual((await client.post('/voxi/memory',headers=headers,json=body)).status,400)
                self.assertEqual((await client.post('/voxi/memory',headers=headers,json={'action':'retire_device','userId':b,'deviceId':A,'confirmDeviceId':A})).status,200)
                with self.assertRaises(ValueError):radio.memory.admit_device(A,'b'*64)

    async def test_user_delete_guards_devices_and_live_tasks_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as directory, closing(AgentStore(directory)) as store:
            radio=VoxiRadio(SimpleNamespace(config={'device_token':'private'},runtime=Path(directory)),'unused')
            radio.agent=SimpleNamespace(store=store)
            uid=radio.memory.bind(A);other=radio.memory.bind(B)
            radio.memory.record(uid,A,'old-turn','我喜欢音乐。')
            task,_=store.submit(uid,A,1,'test-delete','任务历史')
            app=web.Application();app.router.add_post('/voxi/memory',radio.memory_configuration)
            async with TestClient(TestServer(app)) as client:
                headers={'Authorization':'Bearer private'}
                body={'action':'delete_user','userId':uid,'confirmUserId':uid}
                self.assertEqual((await client.post('/voxi/memory',json=body)).status,403)
                self.assertEqual((await client.post('/voxi/memory',headers=headers,json={**body,'confirmUserId':other})).status,400)
                response=await client.post('/voxi/memory',headers=headers,json=body)
                self.assertIn('后台任务',(await response.json())['error'])
                store.update(task['id'],state='completed')
                response=await client.post('/voxi/memory',headers=headers,json=body)
                self.assertIn('绑定设备',(await response.json())['error'])
                radio.memory.change({'action':'rebind','userId':other,'deviceId':A})
                self.assertEqual((await client.post('/voxi/memory',headers=headers,json={**body,'action':'check_delete_user'})).status,200)
                self.assertIsNotNone(radio.memory.user(uid))
                for _ in range(2):self.assertEqual((await client.post('/voxi/memory',headers=headers,json=body)).status,200)
                reopened=MemoryStore(directory)
                self.assertIsNone(reopened.user(uid));self.assertEqual(reopened.bind(A),other)
                self.assertEqual([p['id'] for p in reopened.user_directory()],[other])
                self.assertEqual(reopened.product_metrics()['users'],1)
                self.assertEqual(reopened.snapshot()['turns'],[])
                self.assertEqual(len(reopened.owned_snapshot(uid)['turns']),1)
                self.assertEqual(store.task(task['id'],uid)['state'],'completed')

    async def test_auth_and_background_transcription_clear_race(self):
        with tempfile.TemporaryDirectory() as directory:
            radio=VoxiRadio(SimpleNamespace(config={'device_token':'private'},runtime=Path(directory)),'unused');uid=radio.memory.bind(A)
            app=web.Application();app.router.add_get('/voxi/memory',radio.memory_configuration);app.router.add_post('/voxi/memory',radio.memory_configuration)
            async with TestClient(TestServer(app)) as client:
                self.assertEqual((await client.get('/voxi/memory')).status,403)
                headers={'Authorization':'Bearer private'}
                self.assertEqual((await client.get('/voxi/memory',headers={**headers,'Origin':'https://other'})).status,403)
                self.assertEqual((await (await client.get('/voxi/memory',headers=headers)).json())['users'][0]['id'],uid)
                session=SimpleNamespace(memory_user=uid,memory_device=A,session_id='fixture',turn=1)
                await radio.memory_lock.acquire()
                with patch.object(radio,'transcribe_memory',return_value='我喜欢音乐'):
                    radio.capture_memory(session,b'pcm')
                    response=await client.post('/voxi/memory',headers=headers,json={'action':'clear','userId':uid});self.assertEqual(response.status,200)
                    radio.memory_lock.release();await asyncio.gather(*radio.memory_tasks)
                self.assertEqual(radio.memory.snapshot()['turns'],[])
                session.turn=2
                with patch.object(radio,'transcribe_memory',return_value='我喜欢科普'):
                    radio.capture_memory(session,b'pcm');await asyncio.gather(*radio.memory_tasks)
                self.assertIn('科普',radio.memory.context(uid))
                with patch.object(radio,'transcribe_memory',side_effect=RuntimeError('fixture')):
                    radio.capture_memory(session,b'pcm');await asyncio.gather(*radio.memory_tasks)
                self.assertIn('转写失败',radio.memory_error)
