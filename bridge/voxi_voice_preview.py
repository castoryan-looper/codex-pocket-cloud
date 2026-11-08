"""Bounded, authenticated fixed-sentence audio preview on the device voice path."""
import asyncio
import base64
from collections import OrderedDict
import contextlib
import hmac
import io
import json
import logging
import re
import time
import wave
from aiohttp import web
from voxi_voices import PREVIEW_TEXT,validate_voice


class Capture:
    def __init__(self):
        self.closed=False;self.pcm=bytearray();self.done=asyncio.Event();self.error=False
    async def send_json(self, packet):
        if packet.get('type')=='error':self.error=True;self.done.set()
        if packet.get('type')=='audio_end':self.done.set()
    async def send_bytes(self, data):
        if len(self.pcm)+len(data)>12*48000:raise ValueError('Preview audio exceeded 12 seconds')
        self.pcm.extend(data)
    async def close(self, **kwargs):self.closed=True;self.done.set()


class VoicePreview:
    def __init__(self, radio):
        self.radio=radio;self.busy=False;self.attempts={};self.cache=OrderedDict()
        self.session_factory=None  # Optional controlled audio fixture in tests.

    async def render(self, voice, owner):
        voice=validate_voice(voice);now=time.monotonic()
        cached=self.cache.get(voice)
        if cached and now-cached[0]<600:
            self.cache.move_to_end(voice);return {**cached[1],'cached':True}
        if self.busy or len(self.radio.active)>=self.radio.max_sessions:
            raise web.HTTPTooManyRequests(text='当前语音资源正在使用，请稍后再试听')
        if now-self.attempts.get(owner,-100)<2:raise web.HTTPTooManyRequests(text='请稍候片刻再试听其他音色')
        self.busy=True;self.attempts[owner]=now
        if len(self.attempts)>256:self.attempts.pop(next(iter(self.attempts)))
        from voxi import VoiceSession
        capture=Capture();session=(self.session_factory or VoiceSession)(capture,self.radio.binary)
        session.preview_mode=True;session.voice_override=voice;session.settings_source=self.radio.settings
        self.radio.active.add(session)
        try:
            async def generate():
                await session.start()
                await session.say_agent(PREVIEW_TEXT)
                await capture.done.wait()
            await asyncio.wait_for(generate(),80)
            if capture.error or len(capture.pcm)<4800:raise RuntimeError('No complete preview audio')
            stream=io.BytesIO()
            with wave.open(stream,'wb') as output:
                output.setnchannels(1);output.setsampwidth(2);output.setframerate(24000);output.writeframes(capture.pcm)
            result={'voice':voice,'text':PREVIEW_TEXT,'audioFormat':'wav','sampleRate':24000,
                    'audioBase64':base64.b64encode(stream.getvalue()).decode(),'cached':False}
            self.cache[voice]=(time.monotonic(),result)
            while len(self.cache)>4:self.cache.popitem(last=False)
            return result
        except Exception as e:
            logging.getLogger('pocket.voxi').warning('Voxi preview failed voice=%s phase=%s errorType=%s',voice,getattr(session,'phase','unknown'),type(e).__name__)
            raise
        finally:
            try:
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
            if not isinstance(body,dict) or not set(body)<={'voice','actorUserId'}:raise ValueError('试听只接受音色，不接受自定义文本或其他设置')
            owner=body.get('actorUserId','admin')
            if owner!='admin' and (not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{32}',owner) or not r.memory or not r.memory.user(owner)):
                raise ValueError('用户档案无效')
            voice=body.get('voice')
            if voice=='':voice=r.settings.snapshot()['voice']
            job=asyncio.create_task(self.render(voice,owner))
            # Close the upstream session if the browser/gateway leaves mid-generation.
            try:
                while not job.done():
                    if request.transport is None or request.transport.is_closing():raise asyncio.CancelledError()
                    await asyncio.wait({job},timeout=.25)
                return response(200,await job)
            finally:
                if not job.done():job.cancel();await asyncio.gather(job,return_exceptions=True)
        except (ValueError,TypeError):return response(400,{'error':'音色或试听请求无效'})
        except web.HTTPTooManyRequests as e:return response(429,{'error':e.text})
        except asyncio.CancelledError:raise
        except Exception:return response(502,{'error':'未能生成试听。当前音色可能不被账户支持，或语音服务暂不可用；未修改你的设置。'})
