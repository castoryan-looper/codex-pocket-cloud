"""Bounded, authenticated fixed-sentence audio preview on the device voice path."""
import asyncio
import base64
from collections import OrderedDict
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import time
import wave
from aiohttp import web
from voxi_voices import PREVIEW_TEXT,VOICES,validate_voice

MAX_SECONDS=20
# Text/protocol/catalog changes create a new library, never reuse old-language clips.
LIBRARY_VERSION=hashlib.sha256(('codex-v3-pcm24k-v2\n'+PREVIEW_TEXT+'\n'+','.join(VOICES)).encode()).hexdigest()[:16]


class Capture:
    def __init__(self):
        self.closed=False;self.pcm=bytearray();self.done=asyncio.Event();self.error=False;self.complete=False
    async def send_json(self, packet):
        if packet.get('type')=='error':self.error=True;self.done.set()
        if packet.get('type')=='audio_end':self.complete=True;self.done.set()
    async def send_bytes(self, data):
        if len(self.pcm)+len(data)>MAX_SECONDS*48000:raise ValueError('Preview audio exceeded duration limit')
        self.pcm.extend(data)
    async def close(self, **kwargs):self.closed=True;self.done.set()


class VoicePreview:
    def __init__(self, radio):
        self.radio=radio;self.busy=False;self.attempts={};self.cache=OrderedDict()
        self.session_factory=None  # Optional controlled audio fixture in tests.
        runtime=getattr(radio.owner,'runtime',None)
        self.directory=Path(runtime)/'voice-previews'/LIBRARY_VERSION if runtime else None
        self.worker=None;self.current=None;self.waiting=False;self.failures={};self.ready=set()
        if self.directory:
            self.directory.mkdir(parents=True,exist_ok=True)
            try:
                failures=json.loads((self.directory/'failures.json').read_text(encoding='utf-8'))
                self.failures={v:'生成失败；可由管理员重试' for v in VOICES if v in failures}
            except (OSError,ValueError,TypeError):pass
            for voice in VOICES:
                if self._read(voice) is not None:self.ready.add(voice);self.failures.pop(voice,None)

    def _read(self,voice):
        if not self.directory:return None
        try:
            path=self.directory/(voice+'.wav')
            if not 44<=path.stat().st_size<=MAX_SECONDS*48000+44:return None
            raw=path.read_bytes()
            with wave.open(io.BytesIO(raw),'rb') as wav:
                if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getcomptype())!=(1,2,24000,'NONE'):return None
                frames=wav.getnframes()
                if not 2400<=frames<=MAX_SECONDS*24000 or len(wav.readframes(frames))!=frames*2:return None
            return raw
        except (OSError,ValueError,EOFError,wave.Error):return None

    def _write(self,path,raw):
        temporary=path.with_suffix(path.suffix+'.tmp')
        try:
            with os.fdopen(os.open(temporary,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600),'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            with contextlib.suppress(OSError):temporary.unlink()

    def _save_failures(self):
        if self.directory:self._write(self.directory/'failures.json',json.dumps(self.failures,ensure_ascii=False).encode())

    def _result(self,voice,raw,cached=True):
        return {'voice':voice,'text':PREVIEW_TEXT,'audioFormat':'wav','sampleRate':24000,
                'audioBase64':base64.b64encode(raw).decode(),'cached':cached,'libraryVersion':LIBRARY_VERSION,
                'languages':['zh-CN','en']}

    def stored(self,voice):
        raw=self._read(voice)
        if raw is not None:self.ready.add(voice);return self._result(voice,raw)
        self.ready.discard(voice)
        cached=self.cache.get(voice)
        if cached and not self.directory:return {**cached[1],'cached':True}
        return None

    def status(self):
        return {'version':LIBRARY_VERSION,'previewText':PREVIEW_TEXT,'total':len(VOICES),
                'ready':len(self.ready),'failed':len(self.failures),'running':bool(self.worker and not self.worker.done()),
                'current':self.current,'waitingForCapacity':self.waiting,'persistent':self.directory is not None,
                'voices':[{'id':v,'state':'ready' if v in self.ready else 'failed' if v in self.failures else 'generating' if v==self.current else 'pending'} for v in VOICES]}

    def start(self,retry=False):
        if not self.directory:return  # Production uses the existing persistent runtime volume.
        if retry:
            self.failures.clear();self._save_failures()
        if not self.worker or self.worker.done():self.worker=asyncio.create_task(self._prepare())

    async def close(self):
        if self.worker and not self.worker.done():
            self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)

    async def _prepare(self):
        try:
            default=self.radio.settings.snapshot()['voice']
            for voice in dict.fromkeys((default,*VOICES)):
                if self.stored(voice) or voice in self.failures:continue
                self.current=voice
                while True:
                    self.waiting=self.busy or len(self.radio.active)>=self.radio.max_sessions
                    if self.waiting:await asyncio.sleep(2);continue
                    try:
                        await self.render(voice,'library');break
                    except web.HTTPTooManyRequests:await asyncio.sleep(2)
                    except asyncio.CancelledError:raise
                    except Exception:
                        self.failures[voice]='生成失败；可由管理员重试';self._save_failures();break
                await asyncio.sleep(.25)
        except asyncio.CancelledError:raise
        except Exception:
            logging.getLogger('pocket.voxi').warning('Voxi preview library stopped; check persistent storage')
        finally:self.current=None;self.waiting=False

    async def render(self, voice, owner):
        voice=validate_voice(voice);now=time.monotonic()
        cached=self.stored(voice)
        if cached:return cached
        if self.busy or len(self.radio.active)>=self.radio.max_sessions:
            raise web.HTTPTooManyRequests(text='当前语音资源正在使用，请稍后再试听')
        if owner!='library' and now-self.attempts.get(owner,-100)<2:raise web.HTTPTooManyRequests(text='请稍候片刻再试听其他音色')
        self.busy=True;self.attempts[owner]=now
        if len(self.attempts)>256:self.attempts.pop(next(iter(self.attempts)))
        from voxi import VoiceSession
        capture=Capture();session=None
        try:
            session=(self.session_factory or VoiceSession)(capture,self.radio.binary)
            session.preview_mode=True;session.voice_override=voice;session.settings_source=self.radio.settings
            self.radio.active.add(session)
            async def generate():
                await session.start()
                await session.say_agent(PREVIEW_TEXT)
                await capture.done.wait()
            await asyncio.wait_for(generate(),80)
            if capture.error or not capture.complete or len(capture.pcm)<4800:raise RuntimeError('No complete preview audio')
            stream=io.BytesIO()
            with wave.open(stream,'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000);output.writeframes(capture.pcm)
            raw=stream.getvalue()
            if self.directory:self._write(self.directory/(voice+'.wav'),raw)
            result=self._result(voice,raw,False)
            self.ready.add(voice);self.failures.pop(voice,None)
            self.cache[voice]=(time.monotonic(),result)
            while len(self.cache)>4:self.cache.popitem(last=False)
            return result
        except Exception as e:
            logging.getLogger('pocket.voxi').warning('Voxi preview failed voice=%s phase=%s errorType=%s',voice,getattr(session,'phase','unknown'),type(e).__name__)
            raise
        finally:
            try:
                if session:
                    with contextlib.suppress(Exception):await session.stop()
            finally:self.radio.active.discard(session);self.busy=False

    async def handle(self, request):
        r=self.radio
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+r.owner.config['device_token']):raise web.HTTPForbidden()
        def response(status,value):return web.json_response(value,status=status,headers={'Cache-Control':'no-store'})
        try:
            raw=await request.read()
            if len(raw)>2048:raise ValueError('试听请求过大')
            body=json.loads(raw)
            if not isinstance(body,dict) or not set(body)<={'voice','actorUserId','action'}:raise ValueError('试听只接受音色，不接受自定义文本或其他设置')
            owner=body.get('actorUserId','admin')
            if owner!='admin' and (not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{32}',owner) or not r.memory or not r.memory.user(owner)):
                raise ValueError('用户档案无效')
            if body.get('action'):
                if set(body)-{'action','actorUserId'}:raise ValueError('请求无效')
                if body['action']=='status':return response(200,self.status())
                if body['action']=='prepare' and owner=='admin':
                    self.start(retry=True);return response(200,self.status())
                raise ValueError('不能执行此项音频库操作')
            voice=body.get('voice')
            if voice=='':voice=r.settings.snapshot()['voice']
            voice=validate_voice(voice)
            value=self.stored(voice)
            if value:return response(200,value)
            if voice in self.failures:return response(409,{'error':'此音色的双语样音生成失败，请管理员在音频库重试。没有替换成其他音色。'})
            self.start()
            return response(409,{'error':'双语样音正在后台准备，稍后再试听；准备完成后无需重新生成。'})
        except (ValueError,TypeError):return response(400,{'error':'音色或试听请求无效'})
        except web.HTTPTooManyRequests as e:return response(429,{'error':e.text})
        except asyncio.CancelledError:raise
        except Exception:return response(502,{'error':'未能生成试听。当前音色可能不被账户支持，或语音服务暂不可用；未修改你的设置。'})
