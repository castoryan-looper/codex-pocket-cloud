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
from voxi_voice_preview import Capture,VoicePreview,MAX_SECONDS
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
        async def preview(request):return await self.radio.voice_preview.handle(request)
        app=web.Application();app.router.add_post('/voxi/voice-preview',preview);app.router.add_post('/voxi/portal',UserPortal(self.radio).handle)
        self.client=TestClient(TestServer(app));await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();await self.radio.voice_preview.close();await self.radio.agent.close();self.tmp.cleanup()
    async def call(self,body,headers=None):
        r=await self.client.post('/voxi/voice-preview',headers=headers if headers is not None else {'Authorization':'Bearer internal'},json=body)
        return r,await r.json() if r.content_type=='application/json' else await r.text()

    async def test_preview_uses_selected_voice_fixed_text_and_isolated_session_without_saving(self):
        before=self.radio.settings.snapshot();prefs=self.radio.agent.store.preferences(self.uid)
        await self.radio.voice_preview.render('ember',self.uid)
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
        with self.assertRaises(RuntimeError):await self.radio.voice_preview.render('ember',self.uid)
        self.assertTrue(self.created[0].stopped)
        self.assertFalse(self.radio.voice_preview.cache);self.assertFalse(self.radio.voice_preview.busy);self.assertEqual(self.radio.settings.snapshot()['voice'],'cove')

    async def test_admission_does_not_interrupt_existing_device_sessions(self):
        existing=SimpleNamespace();self.radio.active={1,2};self.radio.max_sessions=2
        with self.assertRaises(web.HTTPTooManyRequests):await self.radio.voice_preview.render('cove',self.uid)
        self.assertEqual(self.created,[]);self.assertEqual(self.radio.active,{1,2})
        self.radio.active=set()
        r,_=await self.call({'voice':'not-a-voice'});self.assertEqual(r.status,400)

    async def test_preview_auth_and_custom_text_rejection(self):
        for headers in ({},{'Authorization':'Bearer internal','Origin':'https://example.com'}):
            r,_=await self.call({'voice':'cove'},headers);self.assertEqual(r.status,403)
        for body in ({'voice':'cove','text':'Run a task'},{'voice':'marin'},{'voice':'cove','actorUserId':'b'*32}):
            r,_=await self.call(body);self.assertEqual(r.status,400)

    async def test_inherited_preview_resolves_current_service_default(self):
        cfg=self.radio.settings.snapshot();cfg['voice']='maple';self.radio.settings.save(cfg)
        await self.radio.voice_preview.render('maple',self.uid)
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

    async def test_persisted_bilingual_clip_survives_restart_and_never_calls_synthesis_on_click(self):
        await self.radio.voice_preview.render('ember',self.uid)
        preview=VoicePreview(self.radio)
        class Forbidden:
            def __init__(self,*args):raise AssertionError('cache hit invoked synthesis')
        preview.session_factory=Forbidden;self.radio.voice_preview=preview
        # Playback remains available even when all realtime capacity is occupied.
        self.radio.active={1,2}
        r,data=await self.call({'voice':'ember'});self.assertEqual(r.status,200)
        self.assertTrue(data['cached']);self.assertEqual(data['languages'],['zh-CN','en'])
        self.assertIn('你好',data['text']);self.assertIn("Hello, I'm Voxi",data['text'])
        self.assertEqual(preview.status()['ready'],1)

    async def test_batch_resumes_skips_ready_tracks_failures_and_admin_can_retry(self):
        preview=self.radio.voice_preview
        await preview.render('ember',self.uid)
        session=preview.session_factory
        class FailsOne(session):
            async def say_agent(self,text):
                if self.voice_override=='juniper':raise RuntimeError('private error')
                await super().say_agent(text)
        preview.session_factory=FailsOne;preview.start()
        await asyncio.wait_for(preview.worker,15)
        self.assertEqual(preview.status()['ready'],24);self.assertEqual(preview.status()['failed'],1)
        self.assertEqual(len([s for s in self.created if s.voice_override=='ember']),1)
        self.assertTrue(all(s.text==PREVIEW_TEXT for s in self.created if hasattr(s,'text')))
        count=len(self.created)
        restarted=VoicePreview(self.radio);restarted.session_factory=session;self.radio.voice_preview=restarted
        restarted.start();await restarted.worker;self.assertEqual(len(self.created),count)
        r,_=await self.call({'action':'prepare','actorUserId':self.uid});self.assertEqual(r.status,400)
        r,_=await self.call({'action':'prepare'});self.assertEqual(r.status,200)
        await restarted.worker;self.assertEqual(restarted.status()['ready'],25)
        self.assertEqual(len(self.created),count+1);self.assertEqual(restarted.status()['failed'],0)

    async def test_pending_playback_is_immediate_and_worker_waits_for_capacity(self):
        preview=self.radio.voice_preview;self.radio.active={1,2}
        r,data=await self.call({'voice':'cove'});self.assertEqual(r.status,409)
        self.assertIn('后台准备',data['error']);await asyncio.sleep(.02)
        self.assertEqual(self.created,[]);self.assertTrue(preview.status()['waitingForCapacity'])
        r,data=await self.call({'action':'status','actorUserId':self.uid});self.assertEqual(r.status,200)
        self.assertEqual(data['total'],25)

    async def test_corrupt_or_older_library_files_are_not_served(self):
        preview=self.radio.voice_preview
        await preview.render('ember',self.uid)
        (preview.directory/'ember.wav').write_bytes(b'RIFFtruncated')
        (preview.directory/'maple.wav.tmp').write_bytes(b'incomplete')
        old=preview.directory.parent/'old-version';old.mkdir();(old/'cove.wav').write_bytes(b'old sample')
        restarted=VoicePreview(self.radio);self.assertEqual(restarted.status()['ready'],0)
        self.assertIsNone(restarted.stored('ember'));self.assertIsNone(restarted.stored('cove'))

    async def test_transport_closure_without_audio_end_never_persists_a_partial_sample(self):
        preview=self.radio.voice_preview
        class Truncated(preview.session_factory):
            async def say_agent(self,text):
                await self.ws.send_bytes(b'\x10\x00'*2400);await self.ws.close()
        preview.session_factory=Truncated
        with self.assertRaises(RuntimeError):await preview.render('cove',self.uid)
        self.assertFalse((preview.directory/'cove.wav').exists());self.assertFalse(preview.ready)

    async def test_session_creation_failure_releases_generation_lock(self):
        preview=self.radio.voice_preview
        class Broken:
            def __init__(self,*args):raise RuntimeError('fixture construction failure')
        preview.session_factory=Broken
        with self.assertRaises(RuntimeError):await preview.render('cove',self.uid)
        self.assertFalse(preview.busy);self.assertEqual(self.radio.active,set())


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
            with self.assertRaises(ValueError):await c.send_bytes(b'\0'*(MAX_SECONDS*48000+2))
        asyncio.run(check())
