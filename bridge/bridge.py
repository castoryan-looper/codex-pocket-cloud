"""Personal Codex hardware client. Personal headless Codex gateway; no model API key required by default.

Authenticated Wi-Fi WebSocket devices use the action dispatcher; no USB bridge.
The HTTP console is loopback-only by default. Runtime files contain private data.
"""
from __future__ import annotations
import argparse
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
import secrets
import shutil
import struct
import time
import wave
from functools import lru_cache

from aiohttp import web, WSMsgType
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
ICONS = json.loads((ROOT/'icons.json').read_text('utf-8'))
LOG = logging.getLogger('pocket')


def _ulaw_sample(value):
    value = (~value) & 0xff
    magnitude = (((value & 15) << 3) + 132) << ((value >> 4) & 7)
    return 132 - magnitude if value & 128 else magnitude - 132

# The device sends 8 kHz G.711 mu-law. Duplicate samples to restore 16 kHz PCM
# expected by the existing speech pipeline; each byte represents four PCM bytes.
_MULAW_16K = tuple(struct.pack('<hh', _ulaw_sample(i), _ulaw_sample(i)) for i in range(256))
def decode_mulaw8k(data):
    return b''.join(_MULAW_16K[byte] for byte in data)


def font_path():
    override = os.environ.get('POCKET_FONT')
    candidates = [override] if override else [
        'C:/Windows/Fonts/msyh.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError('CJK font missing: install fonts-noto-cjk or set POCKET_FONT')


@lru_cache(maxsize=12)
def ui_font(size):
    return ImageFont.truetype(str(font_path()),size)


def codex_binary():
    if os.environ.get('CODEX_BINARY'):
        return os.environ['CODEX_BINARY']
    # Prefer the actual executable over Windows npm shell wrappers.
    npm = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@openai'
    for path in npm.glob('**/codex.exe'):
        if 'vendor' in path.parts:
            return str(path)
    return shutil.which('codex.exe') or shutil.which('codex.cmd') or shutil.which('codex') or 'codex'


class Rpc:
    def __init__(self, notify):
        self.notify = notify
        self.pending = {}
        self.counter = 0
        self.process = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            codex_binary(), 'app-server', '--listen', 'stdio://',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024,
            creationflags=0x08000000 if os.name == 'nt' else 0)
        self.reader = asyncio.create_task(self.read())
        self.errors = asyncio.create_task(self.drain_errors())
        await self.call('initialize', {'clientInfo': {'name': 'codex_pocket', 'version': '0.1.0'}})
        await self.send({'method': 'initialized', 'params': {}})

    async def send(self, msg):
        if self.process.returncode is not None:
            raise RuntimeError('Codex app-server 已退出，请重启桥接')
        self.process.stdin.write((json.dumps(msg, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def call(self, method, params=None):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            msg = {'id': request_id, 'method': method}
            if params is not None:
                msg['params'] = params
            await self.send(msg)
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(request_id, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if 'id' in msg and 'method' not in msg:
                    future = self.pending.get(msg['id'])
                    if future and not future.done():
                        if 'error' in msg:
                            future.set_exception(RuntimeError(str(msg['error'])))
                        else:
                            future.set_result(msg.get('result', {}))
                else:
                    # Never block the reader on an operation that awaits another RPC.
                    asyncio.create_task(self.notify(msg))
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError('Codex app-server 连接断开'))
            await self.notify({'method': 'pocket/disconnected'})

    async def drain_errors(self):
        while await self.process.stderr.readline():
            pass  # Do not leak account data or arbitrary server logs into the device.

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), 5)


class Pocket:
    def __init__(self, args):
        self.args = args
        self.runtime = Path(os.environ.get('POCKET_STATE_DIR',str(ROOT/'runtime'))).resolve()
        self.runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.config_path = self.runtime / 'settings.json'
        self.config = json.loads(self.config_path.read_text('utf-8')) if self.config_path.exists() else {}
        self.config.setdefault('device_token', secrets.token_urlsafe(32))
        self.config.setdefault('console_token', secrets.token_urlsafe(32))
        self.config.setdefault('speech_backend', 'whisper')
        self.config.setdefault('cwd',os.environ.get('POCKET_WORKSPACE',str((ROOT.parent/'assistant-workspace').resolve())))
        Path(self.config['cwd']).mkdir(parents=True, exist_ok=True)
        self.save()
        self.rpc = Rpc(self.notification)
        self.thread = self.config.get('thread_id')
        self.turn = None
        self.status = '启动中'
        self.detail = '正在连接 Codex'
        self.last_event = time.monotonic()
        self.rate = None
        self.rate_at = 0
        self.tokens = None
        self.context = None
        self.reply = ''
        self.draft = ''
        self.approvals = []
        self.menu = 0
        self.page = 0
        self.device = {}
        self.peers = {}
        self.audio = {}
        self.audio_codecs = {}
        self.voice_busy = False
        self.whisper_model = None
        self.action_lock = asyncio.Lock()
        self.last_frame = None
        self.dirty = True
        self.display_battery = None

    def save(self):
        tmp = self.config_path.with_suffix('.tmp')
        with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w',encoding='utf-8') as output:
            json.dump(self.config,output,ensure_ascii=False,indent=2)
        tmp.chmod(0o600)
        tmp.replace(self.config_path)

    def changed(self, status=None, detail=None):
        if status is not None:
            self.status = status
        if detail is not None:
            self.detail = detail[:1000]
        self.last_event = time.monotonic()
        self.dirty = True

    async def start(self):
        await self.rpc.start()
        account = await self.rpc.call('account/read', {'refreshToken': False})
        if not account.get('account'):
            raise RuntimeError('Codex CLI 未登录。请运行 codex login 后重启。')
        params = {'cwd': self.config['cwd'], 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'}
        if self.thread:
            try:
                result = await self.rpc.call('thread/resume', dict(params, threadId=self.thread))
                self.thread = result['thread']['id']
            except Exception:
                LOG.exception('Cannot resume hardware session')
                raise RuntimeError('保存的硬件会话恢复失败；保留记录，请检查 CLI 登录与权限。')
        else:
            result = await self.rpc.call('thread/start', params)
            self.thread = result['thread']['id']
            self.config['thread_id'] = self.thread
            self.save()
        self.changed('待命', '按住 BOOT 说话；短按执行菜单')
        await self.refresh_limits()
        self.tasks = [asyncio.create_task(self.tick())]
        LOG.info('Hardware session: %s', self.thread)

    async def refresh_limits(self):
        try:
            result = await self.rpc.call('account/rateLimits/read')
            self.rate = result.get('rateLimitsByLimitId') or {'codex': result.get('rateLimits')}
            self.rate_at = time.time()
        except Exception:
            self.rate = None
        self.dirty = True

    async def notification(self, msg):
        method, p = msg.get('method', ''), msg.get('params') or {}
        if p.get('threadId') and p['threadId'] != self.thread:
            return
        if 'id' in msg:
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/tool/requestUserInput'):
                self.approvals.append(msg)
                self.changed('等待确认', p.get('command') or p.get('reason') or '请在管理网页查看完整请求')
            else:
                # Unsupported permission/auth/tool requests must never be silently accepted.
                await self.rpc.send({'id': msg['id'], 'error': {'code': -32601, 'message': 'Unsupported by hardware client'}})
            return
        if method == 'turn/started':
            self.turn = p['turn']['id']
            self.reply = ''
            self.changed('工作中', 'Codex 已开始执行')
        elif method == 'turn/completed':
            self.turn = None
            self.approvals.clear()
            turn = p.get('turn', {})
            state = turn.get('status')
            self.changed({'completed': '已完成', 'interrupted': '已停止', 'failed': '出错'}.get(state, state),
                         (turn.get('error') or {}).get('message') or self.reply[-400:] or '本轮已结束')
        elif method == 'item/started':
            item = p.get('item', {})
            names = {'commandExecution': '执行命令', 'fileChange': '修改文件', 'webSearch': '搜索资料',
                     'mcpToolCall': '调用工具', 'reasoning': '思考中', 'agentMessage': '生成回复'}
            self.changed('工作中', item.get('command') or names.get(item.get('type'), item.get('type', '处理任务')))
        elif method == 'item/agentMessage/delta':
            self.reply = (self.reply + p.get('delta', ''))[-12000:]
            self.changed(detail=self.reply[-400:])
        elif method == 'item/completed':
            item = p.get('item', {})
            if item.get('type') == 'agentMessage' and item.get('text'):
                self.reply = item['text'][-12000:]
            self.last_event = time.monotonic()
        elif method == 'thread/tokenUsage/updated':
            usage = p.get('tokenUsage') or {}
            self.tokens = (usage.get('total') or {}).get('totalTokens')
            last = usage.get('last') or {}
            self.context = {'used': last.get('totalTokens'), 'max': usage.get('modelContextWindow')}
            self.dirty = True
        elif method == 'account/rateLimits/updated':
            self.rate = p.get('rateLimitsByLimitId') or {'codex': p.get('rateLimits')}
            self.rate_at = time.time()
            self.dirty = True
        elif method == 'serverRequest/resolved':
            self.approvals = [a for a in self.approvals if a['id'] != p.get('requestId')]
            self.dirty = True
        elif method == 'error':
            self.changed('出错', (p.get('error') or {}).get('message', 'Codex 请求失败'))
        elif method == 'pocket/disconnected':
            self.changed('连接断开', 'Codex 服务退出，请重启桥接')

    def menu_items(self):
        if self.approvals:
            return [('查看确认', 'review'), ('拒绝请求', 'decline'), ('停止任务', 'stop')]
        if self.draft:
            return [('发送语音文字', 'send_draft'), ('取消草稿', 'discard'), ('切换页面', 'page')]
        return [('切换页面', 'page'), ('继续未完成工作', 'continue'), ('停止任务', 'stop'), ('刷新额度', 'refresh')]

    async def action(self, action, payload=None):
        payload = payload or {}
        async with self.action_lock:
            if action == 'b2_click':
                self.menu = (self.menu + 1) % len(self.menu_items())
                self.dirty = True
            elif action == 'b1_click':
                selected = self.menu_items()[self.menu % len(self.menu_items())][1]
                await self.execute(selected, payload)
            else:
                await self.execute(action, payload)

    async def execute(self, action, payload):
        if action in ('stop', 'b2_long'):
            if self.turn:
                await self.rpc.call('turn/interrupt', {'threadId': self.thread, 'turnId': self.turn})
                self.changed('正在停止', '等待 Codex 确认停止')
            elif self.approvals:
                await self.decide('decline')
        elif action == 'continue':
            await self.submit('请继续本会话中尚未完成的工作。先核对完成情况；如果已经全部完成，直接说明结果，不要重复执行或制造新任务。遇到阻塞请明确说明。')
        elif action == 'send':
            await self.submit(str(payload.get('text', '')).strip())
        elif action == 'send_draft':
            text = self.draft
            await self.submit(text)
            self.draft = ''
            self.menu = 0
        elif action == 'discard':
            self.draft = ''
            self.menu = 0
            self.changed(detail='已取消语音草稿')
        elif action == 'page':
            self.page = (self.page + 1) % 3
            self.dirty = True
        elif action == 'refresh':
            await self.refresh_limits()
        elif action == 'review':
            self.changed('等待操作确认', '完整命令在管理网页；设备可拒绝或停止')
        elif action in ('accept', 'decline'):
            await self.decide(action, payload.get('answers'))
        elif action == 'draft':
            self.draft = str(payload.get('text', ''))[:4000]
            self.menu = 0
            self.changed('待发送', self.draft)
        else:
            raise ValueError('未知操作')

    async def decide(self, decision, answers=None):
        if not self.approvals:
            raise ValueError('没有待确认请求')
        request = self.approvals[0]
        if request['method'] == 'item/tool/requestUserInput':
            if decision == 'decline':
                if self.turn:
                    await self.rpc.call('turn/interrupt', {'threadId': self.thread, 'turnId': self.turn})
                return
            if not isinstance(answers, dict):
                raise ValueError('需要填写问题答案')
            result = {'answers': answers}
        else:
            result = {'decision': decision}
        await self.rpc.send({'id': request['id'], 'result': result})
        self.approvals.pop(0)
        self.menu = 0
        self.changed('工作中', '已提交确认结果')

    async def submit(self, text):
        if not text:
            raise ValueError('任务不能为空')
        if self.turn:
            raise ValueError('当前任务正在执行；请先停止或等待完成')
        self.changed('正在提交', text)
        params = {'threadId': self.thread, 'input': [{'type': 'text', 'text': text}]}
        if os.environ.get('POCKET_EXTERNAL_SANDBOX') == 'docker':
            # Docker supplies the outer boundary; do not require nested Linux namespaces.
            params['sandboxPolicy'] = {'type': 'externalSandbox', 'networkAccess': 'enabled'}
        result = await self.rpc.call('turn/start', params)
        if result['turn']['status'] == 'inProgress' and self.status == '正在提交':
            self.turn = result['turn']['id']
            self.changed('工作中', '任务已提交')

    async def device_packet(self, msg, source):
        kind = msg.get('type')
        if kind == 'hello':
            self.device = {'source': 'wifi' if source != 'usb' else 'usb', 'connectionId':source, 'lastSeen': time.time(), **{k: msg[k] for k in ('battery', 'millivolts', 'firmware', 'rssi', 'wifiConnected', 'usbConnected', 'configurationMode', 'configurationUrl', 'configurationSsid') if k in msg}}
            self.dirty = True
        elif kind == 'telemetry':
            self.device.update({k: msg[k] for k in ('battery', 'millivolts', 'rssi', 'displayBusy', 'displayTimeouts', 'screenPhase', 'fullRefreshes', 'partialRefreshes', 'firmware', 'wifiConnected', 'usbConnected', 'configurationMode', 'configurationUrl', 'configurationSsid') if k in msg})
            self.device.update(source='wifi' if source != 'usb' else 'usb', lastSeen=time.time())
            self.dirty = True
        elif kind == 'event':
            try:
                await self.action(msg.get('action'))
            except Exception as exc:
                self.changed(detail=str(exc))
        elif kind == 'audio_start':
            if self.voice_busy:
                self.changed(detail='上一段语音仍在识别，请稍候')
                return
            codec = msg.get('codec', 'pcm16')
            if codec not in ('pcm16', 'mulaw8k'):
                self.changed('录音失败', '设备音频格式不受支持')
                return
            self.draft = ''
            self.audio[source] = bytearray()
            self.audio_codecs[source] = codec
            self.changed('录音中', f'松开 BOOT 结束，最长 {msg.get("maxSeconds",20)} 秒')
        elif kind == 'audio_chunk':
            data = self.audio.get(source)
            if data is not None:
                chunk = base64.b64decode(msg.get('data', ''), validate=True)
                max_bytes = 160000 if self.audio_codecs.get(source) == 'mulaw8k' else 640000
                if len(data) + len(chunk) <= max_bytes:
                    data.extend(chunk)
                else:
                    self.audio.pop(source, None)
                    self.audio_codecs.pop(source, None)
                    self.changed('录音失败', '录音超出 20 秒上限，请重试')
        elif kind == 'audio_end':
            data = self.audio.pop(source, None)
            codec = self.audio_codecs.pop(source, 'pcm16')
            if data is not None:
                if msg.get('cancelled'):
                    self.changed('已取消', '录音已取消，未提交识别')
                elif msg.get('error'):
                    self.changed('录音失败','麦克风采集或上传队列中断，请重试')
                elif msg.get('bytes') is not None and msg['bytes'] != len(data) * (4 if codec == 'mulaw8k' else 1):
                    self.changed('录音失败','音频传输不完整，请重试')
                else:
                    asyncio.create_task(self.transcribe(decode_mulaw8k(data) if codec == 'mulaw8k' else bytes(data)))
        elif kind == 'frame_ack':
            self.device['frameAck'] = time.time()
            self.device['frameHash'] = msg.get('hash')
            self.device.update({k:msg[k] for k in ('wifiConnected','usbConnected','configurationMode') if k in msg})
        elif kind == 'diagnostic':
            self.device['diagnostic'] = {k:v for k,v in msg.items() if k != 'type'}

    async def transcribe(self, data):
        if self.voice_busy:
            self.changed(detail='上一段语音仍在识别，请稍候')
            return
        if len(data) < 6400:
            self.changed('录音太短','按住 BOOT，看到录音中后说话，松开结束')
            return
        self.voice_busy = True
        import array, math
        samples = array.array('h', data[:len(data)//2*2])
        rms = math.sqrt(sum(s*s for s in samples)/len(samples))
        self.device['lastRecording'] = {'seconds':round(len(data)/32000,2),'rms':round(rms,1),'peak':max(abs(s) for s in samples)}
        LOG.info('Voice capture: %.2fs RMS=%.1f',len(data)/32000,rms)
        path = self.runtime / 'last-recording.wav'
        try:
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(data)
            if self.config.get('speech_backend') == 'windows':
                self.changed('识别语音', '正在使用 Windows 离线听写')
                process = await asyncio.create_subprocess_exec('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(ROOT / 'speech.ps1'), '-WavePath', str(path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    raise RuntimeError('语音识别超时，请重录')
                if process.returncode:
                    raise RuntimeError('Windows 中文语音识别失败，请检查 zh-CN 语音组件')
                text = stdout.decode('utf-8-sig').strip()
            else:
                self.changed('识别语音', 'Whisper 正在本机识别中文')
                text = await asyncio.wait_for(asyncio.to_thread(self.whisper_transcribe, data),90)
            if not text:
                raise ValueError(f'录到 {len(data)/32000:.1f} 秒，但未识别出文字。请在“录音中”出现后说话，松开结束')
            self.draft = text[:4000]
            self.menu = 0
            self.changed('待发送', self.draft)
        except Exception as exc:
            self.changed('语音失败', str(exc))
        finally:
            self.voice_busy = False
            path.unlink(missing_ok=True)

    def whisper_transcribe(self, data):
        import numpy as np
        from faster_whisper import WhisperModel
        model_path = Path(os.environ.get('POCKET_MODEL_DIR',str(ROOT.parent/'models/whisper-small')))
        if not (model_path/'model.bin').exists():
            raise RuntimeError('本地语音模型未安装，请先运行 setup-speech.py')
        if self.whisper_model is None:
            self.whisper_model = WhisperModel(str(model_path),device='cpu',compute_type='int8',cpu_threads=int(os.environ.get('POCKET_ASR_THREADS','4')),local_files_only=True)
        pcm = np.frombuffer(data,dtype='<i2').astype(np.float32)/32768.0
        segments, _ = self.whisper_model.transcribe(pcm,language='zh',beam_size=3,
            condition_on_previous_text=False,vad_filter=True,
            vad_parameters={'min_speech_duration_ms':150,'min_silence_duration_ms':400,'threshold':0.35})
        return ''.join(segment.text for segment in segments).strip()

    def remaining(self):
        if not self.rate or time.time() - self.rate_at > 360:
            return []
        windows = []
        for name, bucket in self.rate.items():
            if not bucket:
                continue
            for key in ('primary', 'secondary'):
                window = bucket.get(key)
                if window and window.get('usedPercent') is not None:
                    minutes = window.get('windowDurationMins')
                    label = f'{minutes // 60}h' if minutes and minutes < 1440 else (f'{minutes // 1440}d' if minutes else key)
                    windows.append({'bucket': name, 'label': label, 'remaining': max(0, min(100, 100-window['usedPercent'])), 'resetsAt': window.get('resetsAt')})
        return windows

    def snapshot(self):
        host_path = self.runtime/'mobile-host.txt'
        roots = ROOT.parent/'certificates'
        mobile_host = host_path.read_text('utf-8-sig').strip() if host_path.exists() else ''
        mobile_ca = ''.join(p.read_text('utf-8') for p in (roots/'gtsr4.pem', roots/'isrgrootx1.pem') if p.exists())
        device = dict(self.device)
        device['bridgeConnected'] = bool(self.peers) and time.time()-device.get('frameAck',0)<20
        return {'status': self.status, 'detail': self.detail, 'threadId': self.thread, 'turnId': self.turn,
                'reply': self.reply, 'draft': self.draft, 'approvals': self.approvals, 'quota': self.remaining(),
                'quotaUpdatedAt': self.rate_at, 'tokens': self.tokens, 'context': self.context,
                'device': device, 'cwd': self.config['cwd'], 'noProgressSeconds': int(time.monotonic()-self.last_event),
                'mobileHost': mobile_host, 'mobileCa': mobile_ca}

    def render(self):
        im = Image.new('1', (200, 200), 1)
        d = ImageDraw.Draw(im)
        font = ui_font
        def text(x, y, value, size=14):
            d.text((x,y), str(value), font=font(size), fill=0)
        def lines(value, y, count=4):
            row = ''
            number = 0
            for char in str(value).replace('\r',''):
                if char == '\n' or d.textlength(row+char, font=font(14)) > 184:
                    text(8, y+number*20, row)
                    number += 1
                    row = ''
                    if number >= count:
                        return
                    if char == '\n':
                        continue
                row += char
            if number < count:
                text(8,y+number*20,row)
        battery = self.device.get('battery')
        if battery is None or self.display_battery is None or abs(battery-self.display_battery)>=2:
            self.display_battery = battery
        battery = self.display_battery
        text(7,4,'CODEX',12)
        for i,name in enumerate(('wifi','usb')):
            value=self.device.get(name+'Connected')
            rows=ICONS[name+('_on' if value else '_off')]
            for y,row in enumerate(rows):
                for x in range(16):
                    if row & (0x8000>>x): d.point((80+i*28+x,4+y),fill=0)
            if value is None:
                d.rectangle((84+i*28,4,92+i*28,19),fill=1)
                text(84+i*28,3,'?',12)
        text(145,4,f'{battery}%' if battery is not None else '--%',12)
        d.line((6,25,194,25),fill=0)
        text(8,29,self.status[:10],20)
        age = int(time.monotonic()-self.last_event)
        if self.turn and age > 120:
            text(8,58,f'{age}s 无新事件',13)
        elif self.draft:
            text(8,58,'语音草稿：确认后发送',12)
        else:
            windows = self.remaining()
            text(8,58,'  '.join(f'{q["label"]}余{q["remaining"]:g}%' for q in windows[:2]) or '订阅额度：未知',12)
        if self.page == 1:
            lines(f'累计 token: {self.tokens if self.tokens is not None else "未知"}\n电池: {self.device.get("millivolts", "--")}mV（估算）\n工作目录: {Path(self.config["cwd"]).name}',81)
        elif self.page == 2:
            lines('BOOT短按：执行\nBOOT按住1秒：录音\n松开BOOT：结束录音\n双键长按：网页配置',81)
        else:
            lines(self.draft or self.detail,81)
        d.line((6,166,194,166),fill=0)
        selected = self.menu_items()[self.menu % len(self.menu_items())][0]
        text(8,170,'> '+selected,13)
        text(8,188,'PWR 选择   BOOT 执行',10)
        return im

    async def tick(self):
        last_limits = time.monotonic()
        last_frame_at = 0
        while True:
            try:
                now = time.monotonic()
                if now-last_limits > 120:
                    last_limits = now
                    await self.refresh_limits()
                if now-last_frame_at > (0.35 if self.dirty else 4):
                    frame = self.render().tobytes()
                    # Even identical screens carry heartbeat. E-paper only refreshes changed pixels.
                    packet = {'type':'frame', 'data':base64.b64encode(frame).decode(), 'hash':hashlib.sha256(frame).hexdigest()[:12],
                              'state':self.status,'voiceBusy':self.voice_busy,'draftReady':bool(self.draft),
                              'audioCodecs':['pcm16','mulaw8k']}
                    for peer in list(self.peers.values()):
                        with contextlib.suppress(Exception):
                            await peer.send_json(packet)
                    self.render().save(self.runtime/'screen.png')
                    last_frame_at = now
                    self.dirty = False
                await asyncio.sleep(0.2)
            except Exception:
                LOG.exception('Background update failed')
                await asyncio.sleep(2)

    async def websocket(self, request):
        ws = web.WebSocketResponse(protocols=('arduino',),max_msg_size=16000, heartbeat=20)
        await ws.prepare(request)
        source = secrets.token_hex(8)
        try:
            first = await asyncio.wait_for(ws.receive_json(), 10)
            if not hmac.compare_digest(str(first.get('token','')), self.config['device_token']):
                await ws.close(code=1008, message=b'unauthorized')
                return ws
            self.peers[source] = ws
            await self.device_packet(first, source)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self.device_packet(json.loads(message.data), source)
        except (ValueError, asyncio.TimeoutError):
            await ws.close(code=1008)
        finally:
            self.peers.pop(source, None)
            self.audio_codecs.pop(source, None)
            if self.audio.pop(source, None) is not None:
                self.changed('录音中断','Wi-Fi 连接断开，请重新录音')
        return ws

    async def console(self, request):
        return web.FileResponse(ROOT/'console.html')

    def check_console(self, request):
        # Tunnel exposes /device only. The console and connection export remain on loopback,
        # require a per-install bearer token, and reject cross-origin browser calls.
        if request.remote not in ('127.0.0.1', '::1') or request.headers.get('X-Forwarded-For'):
            raise web.HTTPForbidden()
        if request.host.split(':')[0] not in ('127.0.0.1','localhost'):
            raise web.HTTPForbidden()
        if not hmac.compare_digest(request.headers.get('Authorization',''), 'Bearer '+self.config['console_token']):
            raise web.HTTPUnauthorized()
        origin = request.headers.get('Origin')
        if origin and origin != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden()

    async def state(self, request):
        self.check_console(request)
        return web.json_response(self.snapshot())

    async def preview(self, request):
        self.check_console(request)
        data = io.BytesIO()
        self.render().save(data, 'PNG')
        return web.Response(body=data.getvalue(), content_type='image/png')

    async def command(self, request):
        self.check_console(request)
        try:
            msg = await request.json()
            await self.action(msg['action'], msg)
            return web.json_response({'ok': True})
        except Exception as exc:
            return web.json_response({'error':str(exc)}, status=400)

    async def connection_package(self, request):
        self.check_console(request)
        root = ROOT.parent/'certificates'
        host_path = self.runtime/'mobile-host.txt'
        ca_file=os.environ.get('POCKET_CA_FILE')
        ca=Path(ca_file).read_text('utf-8') if ca_file else ''.join(p.read_text('utf-8') for p in (root/'gtsr4.pem',root/'isrgrootx1.pem') if p.exists())
        host=os.environ.get('POCKET_PUBLIC_HOST') or (host_path.read_text('utf-8-sig').strip() if host_path.exists() else '')
        package={'host':host,'port':int(os.environ.get('POCKET_PUBLIC_PORT','443')),'tls':True,'path':'/device','token':self.config['device_token'],'ca':ca}
        return web.json_response(package,headers={'Content-Disposition':'attachment; filename="codex-pocket-connection.json"','Cache-Control':'no-store'})

    async def health(self, request):
        process=getattr(self.rpc,'process',None)
        ready=process is not None and process.returncode is None
        return web.json_response({'ok':ready,'deviceConnected':bool(self.peers)},status=200 if ready else 503)

    def app(self):
        app = web.Application(client_max_size=16000)
        app.add_routes([web.get('/',self.console),web.get('/device',self.websocket),web.get('/healthz',self.health),
                        web.get('/api/state',self.state),web.get('/api/screen',self.preview),
                        web.post('/api/action',self.command),web.get('/api/connection',self.connection_package)])
        return app


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--http-port',type=int,default=8787)
    parser.add_argument('--open',action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    pocket = Pocket(args)
    # Reopening the console must not attach a second app-server to an active thread.
    import socket
    with socket.socket() as probe:
        probe.settimeout(0.3)
        occupied = probe.connect_ex(('127.0.0.1',args.http_port)) == 0
    if occupied:
        if args.open and (pocket.runtime/'console-url.txt').exists():
            import webbrowser
            webbrowser.open((pocket.runtime/'console-url.txt').read_text('utf-8'))
            return
        raise RuntimeError(f'端口 {args.http_port} 已在使用。已有桥接时请打开 open-console.ps1。')
    runner = web.AppRunner(pocket.app(),access_log=None)
    try:
        await pocket.start()
        await runner.setup()
        await web.TCPSite(runner,args.host,args.http_port).start()
        url = f'http://127.0.0.1:{args.http_port}/#'+pocket.config['console_token']
        # Store only locally; the token is not printed in logs or included in HTTP URLs.
        (pocket.runtime/'console-url.txt').write_text(url,'utf-8')
        LOG.info('Console ready at http://127.0.0.1:%d/ (open via start.ps1)',args.http_port)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        for task in getattr(pocket,'tasks',[]):
            task.cancel()
        await pocket.rpc.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
