import asyncio
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import AsyncMock,patch
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from voxi import VoxiRadio,VoiceSession,frameless_session_update
from voxi_config import VoiceSettings,DEFAULTS
from voxi_voice_preview import Capture
from voxi_voices import VOICES,PREVIEW_TEXT
from voxi_portal import UserPortal


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.radio=VoxiRadio(SimpleNamespace(runtime=Path(self.tmp.name),config={'cwd':self.tmp.name,'device_token':'internal'}),'unused')
        self.uid=self.radio.memory.bind('esp32-111111111111');self.created=[]
        owner=self
        class Session:
            def __init__(self,ws,binary):self.ws=ws;self.memory_user=None;self.stopped=False;owner.created.append(self)
            async def start(self):pass
            async def say_agent(self,text):
                self.text=text
                await self.ws.send_bytes(b'\x10\x00'*2400)
                await self.ws.send_json({'type':'audio_end'})
            async def stop(self):self.stopped=True
        self.radio.voice_preview.session_factory=Session
        app=web.Application();app.router.add_post('/voxi/voice-preview',self.radio.voice_preview.handle);app.router.add_post('/voxi/portal',UserPortal(self.radio).handle)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();await self.radio.agent.close();self.tmp.cleanup()
    async def call(self,body,headers=None):
        r=await self.client.post('/voxi/voice-preview',headers=headers if headers is not None else {'Authorization':'Bearer internal'},json=body)
        return r,await r.json() if r.content_type=='application/json' else await r.text()

    async def test_preview_uses_selected_voice_fixed_text_and_isolated_session_without_saving(self):
        before=self.radio.settings.snapshot();prefs=self.radio.agent.store.preferences(self.uid)
        r,data=await self.call({'voice':'ember','actorUserId':self.uid});self.assertEqual(r.status,200)
        session=self.created[0];self.assertEqual(session.voice_override,'ember');self.assertTrue(session.preview_mode);self.assertTrue(session.stopped)
        self.assertEqual(session.text,PREVIEW_TEXT);self.assertEqual(data['voice'],'ember')
        with wave.open(io.BytesIO(base64.b64decode(data['audioBase64'])),'rb') as wav:
            self.assertEqual(wav.getframerate(),24000);self.assertEqual(wav.getnchannels(),1);self.assertEqual(wav.getnframes(),2400)
        self.assertEqual(self.radio.settings.snapshot(),before);self.assertEqual(self.radio.agent.store.preferences(self.uid),prefs)
        self.assertEqual(len(self.radio.active),0);self.assertFalse(self.radio.voice_preview.busy)
        r,again=await self.call({'voice':'ember','actorUserId':self.uid});self.assertEqual(r.status,200);self.assertTrue(again['cached']);self.assertEqual(len(self.created),1)

    async def test_failed_preview_cleans_up_and_does_not_cache_or_replace_voice(self):
        class BadSession(self.radio.voice_preview.session_factory):
            async def say_agent(self,text):raise RuntimeError('private upstream details')
        self.radio.voice_preview.session_factory=BadSession
        r,data=await self.call({'voice':'ember'});self.assertEqual(r.status,502)
        self.assertNotIn('private upstream',json.dumps(data));self.assertTrue(self.created[0].stopped)
        self.assertFalse(self.radio.voice_preview.cache);self.assertFalse(self.radio.voice_preview.busy);self.assertEqual(self.radio.settings.snapshot()['voice'],'cove')

    async def test_admission_does_not_interrupt_existing_device_sessions(self):
        existing=SimpleNamespace();self.radio.active={1,2};self.radio.max_sessions=2
        r,_=await self.call({'voice':'cove'});self.assertEqual(r.status,429);self.assertEqual(self.created,[]);self.assertEqual(self.radio.active,{1,2})
        self.radio.active=set()
        r,_=await self.call({'voice':'not-a-voice'});self.assertEqual(r.status,400)

    async def test_preview_auth_and_custom_text_rejection(self):
        for headers in ({},{'Authorization':'Bearer internal','Origin':'https://example.com'}):
            r,_=await self.call({'voice':'cove'},headers);self.assertEqual(r.status,403)
        for body in ({'voice':'cove','text':'Run a task'},{'voice':'marin'},{'voice':'cove','actorUserId':'b'*32}):
            r,_=await self.call(body);self.assertEqual(r.status,400)

    async def test_inherited_preview_resolves_current_service_default(self):
        cfg=self.radio.settings.snapshot();cfg['voice']='maple';self.radio.settings.save(cfg)
        r,data=await self.call({'voice':'','actorUserId':self.uid});self.assertEqual(r.status,200);self.assertEqual(data['voice'],'maple')

    async def test_personal_voice_is_separate_from_fast_prompt_and_other_users(self):
        other=self.radio.memory.create_user('Other')['id'];store=self.radio.agent.store
        store.save_preferences(self.uid,{'fastPrompt':'short answers'})
        r=await self.client.post('/voxi/portal',headers={'Authorization':'Bearer internal'},json={'actorUserId':self.uid,'action':'voice_preference','voice':'juniper'})
        self.assertEqual(r.status,200);data=await r.json();self.assertEqual(data['voiceSettings']['effectiveVoice'],'juniper')
        self.assertEqual(store.preferences(self.uid)['fastPrompt'],'short answers');self.assertEqual(store.preferences(other)['voice'],'')
        store.save_preferences(self.uid,{'fastPrompt':'new preference'})
        self.assertEqual(store.preferences(self.uid)['voice'],'juniper')
        with self.assertRaises(ValueError):store.save_preferences(self.uid,{'fastPrompt':'x','voice':'marin'})

    async def test_cancelled_generation_releases_the_child_session(self):
        started=asyncio.Event()
        class Waiting(self.radio.voice_preview.session_factory):
            async def start(self):started.set();await asyncio.Event().wait()
        self.radio.voice_preview.session_factory=Waiting
        job=asyncio.create_task(self.radio.voice_preview.render('cove',self.uid));await started.wait();job.cancel()
        with self.assertRaises(asyncio.CancelledError):await job
        self.assertTrue(self.created[0].stopped);self.assertFalse(self.radio.voice_preview.busy);self.assertEqual(self.radio.active,set())

    async def test_selected_voice_reaches_native_start_and_preview_disables_research(self):
        from aiortc import RTCPeerConnection
        capture=Capture();voice=VoiceSession(capture,'unused');voice.settings_source=self.radio.settings
        cfg=self.radio.settings.snapshot();cfg['voice']='ember';self.radio.settings.save(cfg)
        self.radio.agent.store.save_preferences(self.uid,{'fastPrompt':'','voice':'juniper'})
        voice.agent_radio=self.radio;voice.memory_user=self.uid
        calls=[]
        class Rpc:
            def __init__(self,binary,notify):self.notify=notify
            async def start(self):pass
            async def close(self):pass
            async def call(self,method,params):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':'fixture'}}
                if method=='thread/realtime/start':
                    await self.notify({'method':'thread/realtime/sdp','params':{'threadId':'fixture','sdp':'fixture'}})
                    voice.ready=True
                return {}
        try:
            with patch('voxi.VoiceRpc',Rpc),patch.object(RTCPeerConnection,'setRemoteDescription',new=AsyncMock()):
                await voice.start()
                self.assertEqual(next(p for m,p in calls if m=='thread/realtime/start')['voice'],'juniper')
                calls.clear();voice.agent_radio=None;voice.memory_user=None;voice.preview_mode=True;voice.voice_override='maple'
                await voice.start()
                self.assertEqual(next(p for m,p in calls if m=='thread/realtime/start')['voice'],'maple')
                self.assertEqual(next(p for m,p in calls if m=='thread/start')['config']['web_search'],'disabled')
        finally:await voice.stop()

    async def test_preview_rejects_unexpected_tool_delegation(self):
        capture=Capture();voice=VoiceSession(capture,'unused');voice.preview_mode=True
        await voice.begin_research()
        self.assertTrue(capture.error);self.assertTrue(capture.closed);self.assertFalse(voice.research_pending)


class VoiceConfigurationTests(unittest.TestCase):
    def test_legacy_settings_migrate_and_old_client_does_not_reset_current_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            old=copy.deepcopy(DEFAULTS);old.pop('voice');path=Path(directory)/'voxi-config.json';path.write_text(json.dumps(old),encoding='utf-8')
            settings=VoiceSettings(directory);self.assertEqual(settings.snapshot()['voice'],'cove')
            value=settings.snapshot();value['voice']='ember';settings.save(value)
            old['replyEndSilenceMs']=900;settings.save(old)
            self.assertEqual(settings.snapshot()['voice'],'ember');self.assertEqual(VoiceSettings(directory).snapshot()['voice'],'ember')
        self.assertEqual(frameless_session_update('juniper')['session']['audio']['output']['voice'],'juniper')
        self.assertIn('cove',VOICES);self.assertNotIn('marin',VOICES)
        with self.assertRaises(ValueError):frameless_session_update('marin')

    def test_capture_enforces_bounded_audio(self):
        async def check():
            c=Capture()
            with self.assertRaises(ValueError):await c.send_bytes(b'\0'*(12*48000+2))
        asyncio.run(check())
