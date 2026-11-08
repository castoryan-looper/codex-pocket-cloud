"""Persistent single-language scripts verified against their recorded audio."""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import unicodedata
import wave
from aiohttp import web
from voxi_voices import PREVIEW_TEXT,PREVIEW_SEGMENTS,VOICES,validate_voice

MAX_SECONDS=15
LIBRARY_VERSION=hashlib.sha256(('codex-0.163-v3-rpc-speech-split-asr-exact-v4\n'+PREVIEW_TEXT+'\n'+','.join(VOICES)).encode()).hexdigest()[:16]
ERRORS={
    'content-mismatch':'录音转写与试听文案不符，未作为有效样音发布',
    'language-mismatch':'录音识别到的语言不符或无法确认，未作为该语言样音发布',
    'verification-unavailable':'本地语音转写模型未就绪，暂时无法核对样音',
    'verification-failed':'语音识别未能完成，未作为有效样音发布',
    'audio-too-long':'播报超过单语样音时长上限，可能出现续说或重复',
    'incomplete-audio':'未收到完整的样音结束，可能出现断流',
    'timeout':'语音连接或播报超时',
    'rate-limit':'语音服务额度或请求频率受限',
    'authentication':'语音登录已失效，请检查云端登录',
    'unsupported':'服务拒绝了音色或参数；这不等于该音色不支持某种语言',
    'network':'语音传输失败，请稍后重试',
    'storage':'样音存储失败，请检查持久卷',
    'speech-failed':'语音生成失败，尚未取得可核对的完整录音',
    'speech-submit-failed':'Codex 播报请求提交失败，已停止批量生成',
    'control-rejected':'语音服务拒绝了控制事件，已停止批量生成',
    'upstream-error':'上游语音会话异常关闭，已停止批量生成',
    'unexpected-delegation':'试听被误分派为任务，已停止批量生成',
    'media-receive':'语音媒体接收异常，已停止批量生成',
}
BLOCKING_CODES=frozenset(('verification-unavailable','verification-failed','authentication','rate-limit','storage',
                         'speech-failed','speech-submit-failed','control-rejected','upstream-error',
                         'unexpected-delegation','media-receive'))

class PreviewError(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)

def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).casefold() if c.isalnum())

class Capture:
    def __init__(self):
        self.closed=False;self.pcm=bytearray();self.done=asyncio.Event()
        self.error=False;self.complete=False;self.failure=None
    async def send_json(self,packet):
        if packet.get('type')=='error':self.error=True;self.done.set()
        if packet.get('type')=='audio_end':self.complete=True;self.done.set()
    async def send_bytes(self,data):
        if len(self.pcm)+len(data)>MAX_SECONDS*48000:
            self.failure='audio-too-long';raise PreviewError(self.failure)
        self.pcm.extend(data)
    async def close(self,**kwargs):self.closed=True;self.done.set()

class VoicePreview:
    def __init__(self,radio):
        self.radio=radio;self.busy=False;self.worker=None;self.current=None;self.current_language=None;self.waiting=False
        self.session_factory=None;self.transcriber=None  # Controlled fixtures only.
        self.failures={};self.ready=set();self.available={};self.descriptions={}
        self.mode='probe'
        runtime=getattr(radio.owner,'runtime',None)
        self.root=Path(runtime)/'voice-previews' if runtime else None
        self.directory=self.root/LIBRARY_VERSION if self.root else None
        if self.directory:
            self.directory.mkdir(parents=True,exist_ok=True)
            try:
                saved=json.loads((self.directory/'failures.json').read_text(encoding='utf-8'))
                self.failures={k:v for k,v in saved.items() if k in {self.key(a,b) for a in VOICES for b in PREVIEW_SEGMENTS}
                               and isinstance(v,dict) and v.get('code') in ERRORS}
            except (OSError,ValueError,TypeError,AttributeError):pass
            try:
                saved=json.loads((self.root/'descriptions.json').read_text(encoding='utf-8'))
                self.descriptions={v:self._description(d) for v,d in saved.items() if v in VOICES}
            except (OSError,ValueError,TypeError,AttributeError):pass
            for voice in VOICES:self._scan(voice)

    @staticmethod
    def key(voice,language):return voice+':'+language

    @staticmethod
    def _description(value):
        if not isinstance(value,dict) or set(value)!={'style','useCase'}:raise ValueError('音色说明无效')
        if any(not isinstance(v,str) or len(v)>limit or '\x00' in v for v,limit in [(value['style'],120),(value['useCase'],200)]):raise ValueError('音色说明过长')
        return {k:v.strip() for k,v in value.items()}

    def describe(self,voice,value):
        voice=validate_voice(voice);value=self._description(value);result={**self.descriptions}
        if any(value.values()):result[voice]=value
        else:result.pop(voice,None)
        if not self.root:raise OSError('No persistent runtime')
        self._write(self.root/'descriptions.json',json.dumps(result,ensure_ascii=False).encode())
        self.descriptions=result

    def enrich(self,meta):
        return {**meta,'library':self.status(),'options':[
            {**v,**{k:text for k,text in self.descriptions.get(v['id'],{}).items() if text},
             **({'styleSource':'admin-listening-note'} if v['id'] in self.descriptions else {}),
             'previewLanguages':list(self.available.get(v['id'],()))} for v in meta['options']]}

    def _paths(self,voice,language):
        path=self.directory/(voice+'-'+language+'.wav')
        return path,path.with_suffix('.json')

    def _read(self,voice,language):
        if not self.directory:return None
        try:
            path,meta_path=self._paths(voice,language);meta=json.loads(meta_path.read_text(encoding='utf-8'));script=PREVIEW_SEGMENTS[language]
            if (not isinstance(meta,dict) or meta.get('voice')!=voice or meta.get('language')!=language
                    or meta.get('text')!=script or meta.get('verification')!='recorded-asr-exact-v1'
                    or meta.get('detectedLanguage')!=('zh' if language=='zh-CN' else 'en')
                    or not isinstance(meta.get('languageProbability'),(int,float)) or not .5<=meta['languageProbability']<=1
                    or not isinstance(meta.get('transcript'),str) or normalized(meta['transcript'])!=normalized(script)):return None
            if not 48044<=path.stat().st_size<=MAX_SECONDS*48000+44:return None
            raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=meta.get('sha256'):return None
            with wave.open(io.BytesIO(raw),'rb') as wav:
                if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getcomptype())!=(1,2,24000,'NONE'):return None
                n=wav.getnframes()
                if not 24000<=n<=MAX_SECONDS*24000 or len(wav.readframes(n))!=n*2:return None
            return raw,meta
        except (OSError,ValueError,TypeError,EOFError,wave.Error):return None

    def _scan(self,voice):
        self.available[voice]=tuple(l for l in PREVIEW_SEGMENTS if self._read(voice,l))
        if len(self.available[voice])==len(PREVIEW_SEGMENTS):self.ready.add(voice)
        else:self.ready.discard(voice)
        for language in self.available[voice]:self.failures.pop(self.key(voice,language),None)

    def _write(self,path,raw):
        temporary=path.with_suffix(path.suffix+'.tmp')
        try:
            with os.fdopen(os.open(temporary,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600),'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            with contextlib.suppress(OSError):temporary.unlink()

    def _save_failures(self):
        if self.directory:self._write(self.directory/'failures.json',json.dumps(self.failures).encode())

    @staticmethod
    def _wav(pcm):
        stream=io.BytesIO()
        with wave.open(stream,'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(pcm)
        return stream.getvalue()

    def stored(self,voice,language=None):
        self._scan(voice)
        clips=[(l,self._read(voice,l)) for l in ([language] if language else PREVIEW_SEGMENTS)]
        clips=[(l,c) for l,c in clips if c]
        if not clips:return None
        pcm=bytearray()
        for _,(raw,_) in clips:
            if pcm:pcm.extend(bytes(16800))
            with wave.open(io.BytesIO(raw),'rb') as wav:pcm.extend(wav.readframes(wav.getnframes()))
        scripts=[c[1] for _,c in clips]
        return {'voice':voice,'text':' '.join(c['text'] for c in scripts),'transcript':' '.join(c['transcript'] for c in scripts),
                'audioFormat':'wav','sampleRate':24000,'audioBase64':base64.b64encode(self._wav(pcm)).decode(),
                'cached':True,'libraryVersion':LIBRARY_VERSION,'languages':[l for l,_ in clips],
                'verification':'recorded-asr-exact-v1','segments':[{k:c[k] for k in ('language','text','transcript')} for c in scripts]}

    def status(self):
        rows=[]
        for voice in VOICES:
            available=self.available.get(voice,());samples=[]
            for language in PREVIEW_SEGMENTS:
                failure=self.failures.get(self.key(voice,language))
                state='ready' if language in available else 'failed' if failure else 'generating' if (voice,language)==(self.current,self.current_language) else 'pending'
                samples.append({'language':language,'state':state,**({'error':{'code':failure['code'],'message':ERRORS[failure['code']]},'attempts':failure.get('attempts',1)} if failure else {})})
            state='ready' if voice in self.ready else 'partial' if available else 'generating' if voice==self.current else 'failed' if any(s['state']=='failed' for s in samples) else 'pending'
            rows.append({'id':voice,'state':state,'languages':list(available),'samples':samples})
        blocked=self.blocked()
        probe_languages=list(self.available.get('cove',()))
        return {'version':LIBRARY_VERSION,'previewText':PREVIEW_TEXT,'previewScripts':PREVIEW_SEGMENTS,'total':len(VOICES),
                'ready':len(self.ready),'partial':sum(r['state']=='partial' for r in rows),
                'samplesReady':sum(len(r['languages']) for r in rows),'samplesTotal':len(VOICES)*len(PREVIEW_SEGMENTS),
                'failed':sum(any(s['state']=='failed' for s in r['samples']) for r in rows),
                'running':bool(self.worker and not self.worker.done()),'current':self.current,'currentLanguage':self.current_language,
                'waitingForCapacity':self.waiting,'persistent':self.directory is not None,'voices':rows,
                'blockedReason':ERRORS[blocked] if blocked else '', 'mode':self.mode,
                'probe':{'voice':'cove','ready':len(probe_languages),'total':len(PREVIEW_SEGMENTS),
                         'languages':probe_languages,'passed':len(probe_languages)==len(PREVIEW_SEGMENTS)}}

    def blocked(self):
        return next((f['code'] for f in self.failures.values() if f['code'] in BLOCKING_CODES),None)

    def start(self,retry=False,probe_only=False):
        if not self.directory or (self.worker and not self.worker.done()):return
        if not retry and self.blocked():return
        if retry:
            if probe_only:self.failures={k:v for k,v in self.failures.items() if not k.startswith('cove:')}
            else:self.failures.clear()
            self._save_failures()
        self.mode='probe' if probe_only else 'all'
        self.worker=asyncio.create_task(self._prepare(probe_only))

    async def close(self):
        if self.worker and not self.worker.done():self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True)

    @staticmethod
    def failure_code(error,session=None,capture=None):
        if isinstance(error,PreviewError):return error.code
        if capture and capture.failure:return capture.failure
        if isinstance(error,TimeoutError):return 'timeout'
        if isinstance(error,ConnectionError):return 'network'
        if isinstance(error,OSError):return 'storage'
        reason=getattr(error,'reason','') or getattr(session,'preview_error_reason','')
        for label,code in [('authentication','authentication'),('rate-limit','rate-limit'),('access-denied','unsupported'),('unsupported','unsupported'),('invalid-parameters','unsupported'),('timeout','timeout'),('network','network')]:
            if label in reason:return code
        code=getattr(session,'preview_failure_code',None)
        if code in ERRORS:return code
        return 'speech-failed'

    async def _prepare(self,probe_only=False):
        try:
            voices=('cove',) if probe_only else tuple(dict.fromkeys(('cove',self.radio.settings.snapshot()['voice'],*VOICES)))
            for voice in voices:
                # Never fan a broken common path out to all 50 samples.
                if voice!='cove' and 'cove' not in self.ready:return
                for language in PREVIEW_SEGMENTS:
                    key=self.key(voice,language)
                    if self._read(voice,language) or key in self.failures:continue
                    self.current=voice;self.current_language=language;attempt=0
                    while attempt<2:
                        self.waiting=self.busy or len(self.radio.active)>=self.radio.max_sessions
                        if self.waiting:await asyncio.sleep(2);continue
                        try:
                            await self.render(voice,'library',language);break
                        except web.HTTPTooManyRequests:await asyncio.sleep(2);continue
                        except asyncio.CancelledError:raise
                        except Exception as error:
                            attempt+=1;code=self.failure_code(error)
                            if code in BLOCKING_CODES:
                                self.failures[key]={'code':code,'attempts':attempt};self._save_failures();return
                            if attempt<2 and code in ('timeout','network','incomplete-audio','content-mismatch','language-mismatch','speech-failed'):
                                await asyncio.sleep(1);continue
                            self.failures[key]={'code':code,'attempts':attempt};self._save_failures();break
                    await asyncio.sleep(.25)
        except asyncio.CancelledError:raise
        except Exception:logging.getLogger('pocket.voxi').warning('Voxi preview library stopped; check persistent storage')
        finally:self.current=None;self.current_language=None;self.waiting=False

    async def _transcribe(self,pcm,language):
        if self.transcriber:return await self.transcriber(pcm,language)
        try:
            async with self.radio.memory_lock:
                # Detect from the recorded audio; do not force the intended language.
                return await asyncio.to_thread(self.radio.transcribe_memory,pcm,None,True)
        except asyncio.CancelledError:raise
        except Exception as e:raise PreviewError('verification-failed') from e

    async def render(self,voice,owner,language=None):
        voice=validate_voice(voice)
        if language is None:
            for lang in PREVIEW_SEGMENTS:await self.render(voice,owner,lang)
            return self.stored(voice)
        if language not in PREVIEW_SEGMENTS:raise ValueError('试听语言无效')
        if self._read(voice,language):return self.stored(voice,language)
        if self.busy or len(self.radio.active)>=self.radio.max_sessions:raise web.HTTPTooManyRequests(text='当前语音资源正在使用')
        self.busy=True
        from voxi import VoiceSession
        capture=Capture();session=None
        try:
            if not self.transcriber and not (Path(os.environ.get('POCKET_MODEL_DIR','/models/whisper-small'))/'model.bin').exists():raise PreviewError('verification-unavailable')
            session=(self.session_factory or VoiceSession)(capture,self.radio.binary)
            session.preview_mode=True;session.preview_text=PREVIEW_SEGMENTS[language];session.preview_language=language
            session.voice_override=voice;session.settings_source=self.radio.settings
            self.radio.active.add(session)
            async def generate():
                await session.start();await session.say_agent(PREVIEW_SEGMENTS[language]);await capture.done.wait()
            await asyncio.wait_for(generate(),80)
            if capture.failure:raise PreviewError(capture.failure)
            if capture.error:raise PreviewError(self.failure_code(RuntimeError(),session))
            if not capture.complete or len(capture.pcm)<48000:raise PreviewError('incomplete-audio')
            await session.stop();self.radio.active.discard(session)
            session.phase='verifying-audio'
            recognition=await self._transcribe(bytes(capture.pcm),language)
            if not isinstance(recognition,dict):raise PreviewError('verification-failed')
            if recognition.get('language')!=('zh' if language=='zh-CN' else 'en') or not isinstance(recognition.get('probability'),(int,float)) or not .5<=recognition['probability']<=1:raise PreviewError('language-mismatch')
            transcript=recognition.get('text')
            if not isinstance(transcript,str) or normalized(transcript)!=normalized(PREVIEW_SEGMENTS[language]):raise PreviewError('content-mismatch')
            raw=self._wav(capture.pcm)
            meta={'voice':voice,'language':language,'text':PREVIEW_SEGMENTS[language],'transcript':transcript,
                  'verification':'recorded-asr-exact-v1','detectedLanguage':recognition['language'],
                  'languageProbability':recognition['probability'],'sha256':hashlib.sha256(raw).hexdigest()}
            if self.directory:
                path,meta_path=self._paths(voice,language);self._write(path,raw);self._write(meta_path,json.dumps(meta,ensure_ascii=False).encode())
            self.failures.pop(self.key(voice,language),None);self._scan(voice)
            return self.stored(voice,language)
        except asyncio.CancelledError:raise
        except Exception as e:
            code=self.failure_code(e,session,capture)
            logging.getLogger('pocket.voxi').warning('Voxi preview failed voice=%s language=%s phase=%s reason=%s errorType=%s',voice,language,getattr(session,'phase','unknown'),code,type(e).__name__)
            raise PreviewError(code) from e
        finally:
            if session:
                with contextlib.suppress(Exception):await session.stop()
            self.radio.active.discard(session);self.busy=False

    async def handle(self,request):
        r=self.radio
        if request.headers.get('Origin') or not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+r.owner.config['device_token']):raise web.HTTPForbidden()
        def response(status,value):return web.json_response(value,status=status,headers={'Cache-Control':'no-store'})
        try:
            raw=await request.read()
            if len(raw)>2048:raise ValueError('请求过大')
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError('请求无效')
            owner=body.get('actorUserId','admin')
            if owner!='admin' and (not isinstance(owner,str) or not re.fullmatch(r'[a-f0-9]{32}',owner) or not r.memory or not r.memory.user(owner)):raise ValueError('用户档案无效')
            action=body.get('action')
            if action:
                if action in ('status','prepare','probe') and not set(body)-{'action','actorUserId'}:
                    if action in ('prepare','probe'):
                        if owner!='admin':raise ValueError('不能执行此项音频库操作')
                        if action=='prepare' and 'cove' not in self.ready:
                            return response(409,{'error':'请先完成 Cove 中英文两段验证，再开始全库生成。'})
                        self.start(retry=True,probe_only=action=='probe')
                    return response(200,self.status())
                if action=='describe' and owner=='admin' and set(body)<={'action','actorUserId','voice','style','useCase'}:
                    self.describe(body.get('voice'),{'style':body.get('style'),'useCase':body.get('useCase')})
                    return response(200,{'ok':True})
                raise ValueError('不能执行此项音频库操作')
            if set(body)-{'voice','actorUserId','language'}:raise ValueError('试听请求无效')
            voice=body.get('voice');voice=r.settings.snapshot()['voice'] if voice=='' else validate_voice(voice)
            language=body.get('language')
            if language is not None and language not in PREVIEW_SEGMENTS:raise ValueError('试听语言无效')
            value=self.stored(voice,language)
            if value:return response(200,value)
            failure=next((self.failures.get(self.key(voice,l)) for l in ([language] if language else PREVIEW_SEGMENTS) if self.key(voice,l) in self.failures),None)
            if failure:return response(409,{'error':ERRORS[failure['code']]+'。可由管理员重试。','code':failure['code']})
            if self.blocked():return response(409,{'error':'样音准备已暂停：'+ERRORS[self.blocked()]+'。请管理员处理后重试。','code':self.blocked()})
            if 'cove' not in self.ready:
                self.start(probe_only=True)
                return response(409,{'error':'正在先验证 Cove 两段样音；通过后由管理员启动全库生成，不会播放未经核对的录音。'})
            return response(409,{'error':'这款音色的样音尚未准备，请管理员启动全库生成。'})
        except (ValueError,TypeError):return response(400,{'error':'音色、语言或试听请求无效'})
        except asyncio.CancelledError:raise
        except Exception:return response(502,{'error':'样音库暂不可用，请检查持久存储；未修改音色设置。'})
