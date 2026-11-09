import asyncio
import tempfile
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from voxi import VoxiRadio
from voxi_memory import MemoryStore, device_id

A='esp32-111111111111'
B='esp32-222222222222'

class MemoryTests(unittest.TestCase):
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
