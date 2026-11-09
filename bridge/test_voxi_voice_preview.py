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
from voxi_voice_preview import Capture,VoicePreview,MAX_SECONDS,PreviewError
from voxi_voices import VOICES,PREVIEW_TEXT,PREVIEW_SEGMENTS,catalog
from voxi_voice_validation import POLICY
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
                await self.ws.send_bytes(b'\x10\x00'*24000)
                await self.ws.send_json({'type':'audio_end'})
            async def stop(self):self.stopped=True
        self.radio.voice_preview.session_factory=Session
        async def transcribe(pcm,language):return {'text':PREVIEW_SEGMENTS[language],'language':'zh' if language=='zh-CN' else 'en','probability':.99}
        self.radio.voice_preview.transcriber=transcribe
        async def reject(expected,actual,language):return {'policy':POLICY,'equivalent':False,'reason':'different-meaning'}
        self.radio.voice_preview.semantic_checker=reject
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
        self.assertEqual(session.text,PREVIEW_SEGMENTS['zh-CN']);self.assertEqual(data['voice'],'ember')
        with wave.open(io.BytesIO(base64.b64decode(data['audioBase64'])),'rb') as wav:
            self.assertEqual(wav.getframerate(),24000);self.assertEqual(wav.getnchannels(),1);self.assertEqual(wav.getnframes(),56400)
        self.assertEqual(self.radio.settings.snapshot(),before);self.assertEqual(self.radio.agent.store.preferences(self.uid),prefs)
        self.assertEqual(len(self.radio.voice_slots),0);self.assertFalse(self.radio.voice_preview.busy)
        r,again=await self.call({'voice':'ember','actorUserId':self.uid});self.assertEqual(r.status,200);self.assertTrue(again['cached']);self.assertEqual(len(self.created),2)

    async def test_failed_preview_cleans_up_and_does_not_cache_or_replace_voice(self):
        class BadSession(self.radio.voice_preview.session_factory):
            async def say_agent(self,text):raise RuntimeError('private upstream details')
        self.radio.voice_preview.session_factory=BadSession
        with self.assertRaises(RuntimeError):await self.radio.voice_preview.render('ember',self.uid)
        self.assertTrue(self.created[0].stopped)
        self.assertFalse(self.radio.voice_preview.ready);self.assertFalse(self.radio.voice_preview.busy);self.assertEqual(self.radio.settings.snapshot()['voice'],'cove')

    async def test_admission_does_not_interrupt_existing_device_sessions(self):
        existing=SimpleNamespace();self.radio.voice_slots={1,2};self.radio.max_sessions=2
        with self.assertRaises(web.HTTPTooManyRequests):await self.radio.voice_preview.render('cove',self.uid)
        self.assertEqual(self.created,[]);self.assertEqual(self.radio.voice_slots,{1,2})
        self.radio.voice_slots=set()
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
        self.assertTrue(self.created[0].stopped);self.assertFalse(self.radio.voice_preview.busy);self.assertEqual(self.radio.voice_slots,set())

    async def test_selected_voice_reaches_native_start_and_preview_disables_research(self):
        from aiortc import RTCPeerConnection
        capture=Capture();capture.send_str=AsyncMock()
        voice=VoiceSession(capture,'unused');voice.settings_source=self.radio.settings
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
                capture.send_str.assert_awaited_once()
                self.assertEqual(next(p for m,p in calls if m=='thread/start')['config']['web_search'],'disabled')
                events=[];voice.event=events.append;voice.events.emit('open')
                self.assertEqual(events,[frameless_session_update('maple')])
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
        self.radio.voice_slots={1,2}
        r,data=await self.call({'voice':'ember'});self.assertEqual(r.status,200)
        self.assertTrue(data['cached']);self.assertEqual(data['languages'],['zh-CN','en'])
        self.assertIn('你好',data['text']);self.assertIn('Hello, nice',data['text'])
        self.assertEqual(preview.status()['ready'],1)

    async def test_batch_resumes_skips_ready_tracks_failures_and_admin_can_retry(self):
        preview=self.radio.voice_preview
        await preview.render('ember',self.uid)
        session=preview.session_factory
        class FailsOne(session):
            async def say_agent(self,text):
                if self.voice_override=='juniper':raise PreviewError('unsupported')
                await super().say_agent(text)
        preview.session_factory=FailsOne;preview.start()
        await asyncio.wait_for(preview.worker,25)
        self.assertEqual(preview.status()['ready'],24);self.assertEqual(preview.status()['failed'],1)
        self.assertEqual(len([s for s in self.created if s.voice_override=='ember']),2)
        self.assertTrue(all(s.text in PREVIEW_SEGMENTS.values() for s in self.created if hasattr(s,'text')))
        count=len(self.created)
        restarted=VoicePreview(self.radio);restarted.session_factory=session;restarted.transcriber=preview.transcriber;self.radio.voice_preview=restarted
        restarted.start();await restarted.worker;self.assertEqual(len(self.created),count)
        r,_=await self.call({'action':'prepare','actorUserId':self.uid});self.assertEqual(r.status,400)
        r,_=await self.call({'action':'prepare'});self.assertEqual(r.status,200)
        await restarted.worker;self.assertEqual(restarted.status()['ready'],25)
        self.assertEqual(len(self.created),count+2);self.assertEqual(restarted.status()['failed'],0)

    async def test_pending_playback_is_immediate_and_worker_waits_for_capacity(self):
        preview=self.radio.voice_preview;self.radio.voice_slots={1,2}
        r,data=await self.call({'voice':'cove'});self.assertEqual(r.status,409)
        self.assertIn('验证 Cove',data['error']);await asyncio.sleep(.02)
        self.assertEqual(self.created,[]);self.assertTrue(preview.status()['waitingForCapacity'])
        r,data=await self.call({'action':'status','actorUserId':self.uid});self.assertEqual(r.status,200)
        self.assertEqual(data['total'],25)

    async def test_corrupt_or_older_library_files_are_not_served(self):
        preview=self.radio.voice_preview
        await preview.render('ember',self.uid)
        for lang in PREVIEW_SEGMENTS:(preview.directory/('ember-'+lang+'.wav')).write_bytes(b'RIFFtruncated')
        (preview.directory/'maple.wav.tmp').write_bytes(b'incomplete')
        old=preview.directory.parent/'old-version';old.mkdir();(old/'cove.wav').write_bytes(b'old sample')
        restarted=VoicePreview(self.radio);self.assertEqual(restarted.status()['ready'],0)
        self.assertIsNone(restarted.stored('ember'));self.assertIsNone(restarted.stored('cove'))

    async def test_transport_closure_without_audio_end_never_persists_a_partial_sample(self):
        preview=self.radio.voice_preview
        class Truncated(preview.session_factory):
            async def say_agent(self,text):
                await self.ws.send_bytes(b'\x10\x00'*24000);await self.ws.close()
        preview.session_factory=Truncated
        with self.assertRaises(RuntimeError):await preview.render('cove',self.uid)
        self.assertFalse((preview.directory/'cove.wav').exists());self.assertFalse(preview.ready)

    async def test_session_creation_failure_releases_generation_lock(self):
        preview=self.radio.voice_preview
        class Broken:
            def __init__(self,*args):raise RuntimeError('fixture construction failure')
        preview.session_factory=Broken
        with self.assertRaises(RuntimeError):await preview.render('cove',self.uid)
        self.assertFalse(preview.busy);self.assertEqual(self.radio.voice_slots,set())

    async def test_wrong_language_or_paraphrase_is_not_published_but_good_language_is_preserved(self):
        preview=self.radio.voice_preview
        await preview.render('cove',self.uid,'zh-CN')
        async def wrong(pcm,language):return {'text':'This is a different answer','language':'en','probability':.99}
        preview.transcriber=wrong
        with self.assertRaisesRegex(PreviewError,'content-mismatch'):await preview.render('cove',self.uid,'en')
        row=next(v for v in preview.status()['voices'] if v['id']=='cove')
        self.assertEqual(row['state'],'partial');self.assertEqual(row['languages'],['zh-CN'])
        r,value=await self.call({'voice':'cove'});self.assertEqual(r.status,200)
        self.assertEqual(value['text'],PREVIEW_SEGMENTS['zh-CN']);self.assertEqual(value['languages'],['zh-CN'])
        self.assertIsNone(preview._read('cove','en'))

    async def test_expected_words_do_not_pass_if_recording_language_is_wrong_or_uncertain(self):
        preview=self.radio.voice_preview
        async def wrong(pcm,language):return {'text':PREVIEW_SEGMENTS[language],'language':'en','probability':.99}
        preview.transcriber=wrong
        with self.assertRaisesRegex(PreviewError,'language-mismatch'):await preview.render('cove',self.uid,'zh-CN')
        self.assertIsNone(preview.stored('cove'))

    async def test_global_login_or_verifier_failure_pauses_background_and_click_does_not_retry_quota(self):
        preview=self.radio.voice_preview
        preview.failures['cove:zh-CN']={'code':'rate-limit','attempts':1};preview._save_failures()
        preview.start();self.assertIsNone(preview.worker)
        r,value=await self.call({'voice':'ember'});self.assertEqual(r.status,409)
        self.assertEqual(value['code'],'rate-limit');self.assertEqual(self.created,[])
        self.assertIn('受限',preview.status()['blockedReason'])

    async def test_wave_without_verification_metadata_or_with_changed_audio_is_rejected(self):
        preview=self.radio.voice_preview
        await preview.render('cove',self.uid,'zh-CN')
        path,metadata=preview._paths('cove','zh-CN')
        value=json.loads(metadata.read_text(encoding='utf-8'));value['transcript']='different speech';metadata.write_text(json.dumps(value),encoding='utf-8')
        self.assertIsNone(preview._read('cove','zh-CN'))
        value['transcript']=PREVIEW_SEGMENTS['zh-CN'];metadata.write_text(json.dumps(value),encoding='utf-8')
        raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
        self.assertIsNone(preview._read('cove','zh-CN'))

    async def test_description_is_admin_only_persistent_and_shared_with_user_without_changing_voice(self):
        before=self.radio.settings.snapshot()
        body={'action':'describe','voice':'cove','style':'阳光男声','useCase':'工作播报'}
        r,_=await self.call({**body,'actorUserId':self.uid});self.assertEqual(r.status,400)
        r,_=await self.call(body);self.assertEqual(r.status,200)
        preview=VoicePreview(self.radio)
        meta=preview.enrich(catalog())
        self.assertEqual(meta['options'][0]['style'],'阳光男声')
        self.assertEqual(meta['options'][0]['styleSource'],'admin-listening-note')
        self.assertEqual(self.radio.settings.snapshot(),before)
        preview.describe('juniper',{'style':'','useCase':'解释问题'})
        voice=next(v for v in preview.enrich(catalog())['options'] if v['id']=='juniper')
        self.assertTrue(voice['style']);self.assertEqual(voice['useCase'],'解释问题')

    async def test_production_transcription_path_detects_language_without_expected_script_prompt(self):
        preview=self.radio.voice_preview;preview.transcriber=None;calls=[]
        model_path=Path(self.tmp.name)/'asr';model_path.mkdir();(model_path/'model.bin').touch()
        def transcribe(audio,**kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(text=PREVIEW_SEGMENTS['en'],no_speech_prob=0)],SimpleNamespace(language='en',language_probability=.99)
        self.radio.memory_model=SimpleNamespace(transcribe=transcribe)
        with patch.dict('os.environ',{'POCKET_MODEL_DIR':str(model_path)}):
            await preview.render('cove',self.uid,'en')
        self.assertEqual(len(calls),1);self.assertIsNone(calls[0]['language'])
        self.assertFalse(calls[0]['condition_on_previous_text']);self.assertNotIn('initial_prompt',calls[0])
        self.assertEqual(preview.stored('cove')['languages'],['en'])

    async def test_content_failure_has_safe_per_language_reason_and_retry_does_not_regenerate_good_clip(self):
        preview=self.radio.voice_preview
        await preview.render('cove',self.uid,'zh-CN')
        preview.failures[preview.key('cove','en')]={'code':'content-mismatch','attempts':2};preview._save_failures()
        count=len(self.created)
        r,value=await self.call({'voice':'cove','language':'en'});self.assertEqual(r.status,409)
        self.assertEqual(value['code'],'content-mismatch')
        await preview.render('cove',self.uid,'en')
        self.assertEqual(len(self.created),count+1);self.assertIn('cove',preview.ready)

    async def test_preview_uses_the_same_authenticated_speech_rpc_as_device_audio(self):
        c=Capture();s=VoiceSession(c,'unused');s.preview_mode=True;s.preview_language='en';s.preview_text=PREVIEW_SEGMENTS['en']
        s.thread='preview-thread';s.rpc=SimpleNamespace(call=AsyncMock(return_value={}));sent=[]
        s.events=SimpleNamespace(readyState='open',send=lambda raw:sent.append(json.loads(raw)))
        s.spawn=lambda coro:coro.close()
        await s.say_agent(s.preview_text)
        s.rpc.call.assert_awaited_once_with('thread/realtime/appendSpeech',{'threadId':'preview-thread','text':s.preview_text})
        self.assertEqual(sent,[]);self.assertEqual(s.phase,'speech-await-audio')
        self.assertIn(s.preview_text,s.preview_instructions());self.assertIn('English',s.preview_instructions())
        s.preview_mode=False;await s.say_agent(s.preview_text);self.assertEqual(s.rpc.call.await_count,2)

    async def test_probe_generates_only_two_cove_clips_and_full_generation_requires_probe(self):
        preview=self.radio.voice_preview
        r,_=await self.call({'action':'prepare'});self.assertEqual(r.status,409)
        r,_=await self.call({'action':'probe','actorUserId':self.uid});self.assertEqual(r.status,400)
        r,_=await self.call({'action':'probe'});self.assertEqual(r.status,200)
        await preview.worker
        self.assertEqual(len(self.created),2);self.assertTrue(all(s.voice_override=='cove' for s in self.created))
        self.assertEqual(preview.status()['probe']['ready'],2);self.assertTrue(preview.status()['probe']['passed'])
        self.assertEqual(preview.status()['samplesReady'],2)

    async def test_common_protocol_failure_stops_after_first_sample_instead_of_fifty(self):
        preview=self.radio.voice_preview
        class Rejected(preview.session_factory):
            async def say_agent(self,text):
                self.preview_failure_code='control-rejected';await self.ws.send_json({'type':'error'})
        preview.session_factory=Rejected;preview.start();await preview.worker
        self.assertEqual(len(self.created),1);self.assertEqual(preview.blocked(),'control-rejected')
        preview.start();self.assertEqual(len(self.created),1)
        self.assertEqual(preview.status()['samplesReady'],0)
        self.assertEqual(preview.failures['cove:zh-CN']['code'],'control-rejected')

    async def test_probe_script_failure_also_prevents_fanout_even_without_global_error(self):
        preview=self.radio.voice_preview
        async def wrong(pcm,language):return {'text':'different' if language=='zh-CN' else PREVIEW_SEGMENTS[language],'language':'zh' if language=='zh-CN' else 'en','probability':.99}
        preview.transcriber=wrong;preview.start();await preview.worker
        self.assertEqual(len(self.created),3);self.assertTrue(all(s.voice_override=='cove' for s in self.created))
        self.assertFalse(preview.status()['probe']['passed']);self.assertEqual(preview.status()['probe']['ready'],1)

    async def test_control_error_preserves_safe_category_and_first_cause(self):
        s=VoiceSession(Capture(),'unused');s.preview_mode=True
        with self.assertLogs('pocket.voxi',level='WARNING') as logs:
            await s.on_event({'type':'error','error':{'code':'invalid_event','message':'private credential is secret-fixture-value'}})
            s.preview_problem('media-receive',{})
        self.assertEqual(s.preview_failure_code,'control-rejected')
        self.assertNotIn('secret-fixture-value',' '.join(logs.output));self.assertIn('invalid_event',' '.join(logs.output))

    async def test_semantic_paraphrase_is_published_with_actual_transcript_not_reference_script(self):
        preview=self.radio.voice_preview
        actual='Hi there. Hope you are having a lovely day.'
        async def transcribe(pcm,language):return {'text':actual,'language':'en','probability':.99}
        async def accept(expected,transcript,language):
            self.assertEqual(transcript,actual);return {'policy':POLICY,'equivalent':True,'reason':'equivalent'}
        preview.transcriber=transcribe;preview.semantic_checker=accept
        result=await preview.render('cove',self.uid,'en')
        self.assertEqual(result['text'],actual);self.assertEqual(result['transcript'],actual)
        self.assertEqual(result['referenceText'],PREVIEW_SEGMENTS['en'])
        self.assertEqual(result['segments'][0]['contentMatch'],'semantic')
        self.assertEqual(VoicePreview(self.radio).stored('cove')['text'],actual)
        _,path=preview._paths('cove','en');meta=json.loads(path.read_text(encoding='utf-8'))
        meta['transcript']='A different claim';path.write_text(json.dumps(meta),encoding='utf-8')
        self.assertIsNone(VoicePreview(self.radio).stored('cove'))

    async def test_review_outage_preserves_candidate_and_retry_does_not_resynthesize(self):
        preview=self.radio.voice_preview;actual='Hi there, I wish you a lovely day.'
        async def transcribe(pcm,language):return {'text':actual,'language':'en','probability':.99}
        async def unavailable(*args):raise RuntimeError('fixture outage')
        preview.transcriber=transcribe;preview.semantic_checker=unavailable
        with self.assertRaisesRegex(PreviewError,'semantic-verification-unavailable'):await preview.render('cove',self.uid,'en')
        self.assertIsNone(preview.stored('cove'));self.assertIsNotNone(preview._read('cove','en',True))
        async def accept(*args):return {'policy':POLICY,'equivalent':True,'reason':'equivalent'}
        preview.semantic_checker=accept;await preview.render('cove',self.uid,'en')
        self.assertEqual(len(self.created),1);self.assertEqual(preview.stored('cove')['text'],actual)


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
            with self.assertRaises(PreviewError):await c.send_bytes(b'\0'*(MAX_SECONDS*48000+2))
            self.assertEqual(c.failure,'audio-too-long')
        asyncio.run(check())
