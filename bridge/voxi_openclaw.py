"""External agent routing: OpenClaw Responses API and outbound connector leases.

No runtime credential is sent to devices. Claimed work is never reissued after
an ambiguous disconnect. Muse Code and OpenClaw share the queue but have distinct connector scopes.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from voxi_agent_store import LIVE, TERMINAL, EXTERNAL_AGENT_LABELS


def connection_engine(transport):
    return 'muse' if transport=='muse-relay' else 'openclaw'


def endpoint(value):
    if not isinstance(value,str):raise ValueError('OpenClaw 地址无效')
    try:p=urlsplit(value)
    except ValueError:raise ValueError('OpenClaw 地址无效') from None
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('请填写 HTTPS 根地址；家中／内网实例请选择主动连接')
    try:
        if not ipaddress.ip_address(p.hostname).is_global:raise ValueError('内网地址请使用主动连接')
    except ValueError as e:
        if str(e)=='内网地址请使用主动连接':raise
    return value.rstrip('/')


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM,family=family)
        if not rows or any(not ipaddress.ip_address(r[4][0]).is_global for r in rows):
            raise OSError('Direct OpenClaw requires public addresses')
        return [dict(hostname=host,host=r[4][0],port=port,family=r[0],proto=r[2],flags=0) for r in rows]
    async def close(self):pass


class OpenClaw:
    def __init__(self, hub):
        self.hub=hub;self.store=hub.store
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS openclaw_connections(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,transport TEXT NOT NULL,
          url TEXT NOT NULL,agent TEXT NOT NULL,secret TEXT NOT NULL,seen REAL,status TEXT NOT NULL)''')

    def connection(self, ident, user=None, engine=None):
        r=self.store.db.execute('SELECT * FROM openclaw_connections WHERE id=?',(ident,)).fetchone()
        if not r or (user is not None and r['user_id']!=user):return None
        if engine is not None and connection_engine(r['transport'])!=engine:return None
        return dict(r)

    def public(self, user=None):
        result=[]
        for r in self.store.db.execute('SELECT * FROM openclaw_connections'+(' WHERE user_id=?' if user else '')+' ORDER BY name',(user,) if user else ()):
            v={k:r[k] for k in ('id','user_id','name','transport','url','agent','seen','status')}
            v['engine']=connection_engine(r['transport'])
            if r['transport']!='https' and (not r['seen'] or time.time()-r['seen']>30):v['status']='offline'
            v['configured']=True;result.append(v)
        return result

    def create_muse(self, user, name):
        return self._create(user,name,'muse-relay','','main','')

    def create(self, user, name, transport, url='', agent='main', token=''):
        if transport not in ('https','relay'):raise ValueError('连接方式无效')
        return self._create(user,name,transport,url,agent,token)

    def _create(self, user, name, transport, url, agent, token):
        if not isinstance(name,str) or not 1<=len(name.strip())<=64:raise ValueError('请填写连接名称')
        if not isinstance(agent,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',agent):
            raise ValueError('连接方式或 Agent ID 无效')
        ident=secrets.token_hex(16);pair=None
        if transport=='https':
            url=endpoint(url)
            if not isinstance(token,str) or not 1<=len(token)<=4096 or any(c.isspace() for c in token):raise ValueError('请填写 Gateway Token 或密码')
            nonce=secrets.token_bytes(12)
            secret=(nonce+self.store._cipher().encrypt(nonce,token.encode(),ident.encode())).hex()
        else:
            url='';pair=secrets.token_urlsafe(32);secret=hashlib.sha256(pair.encode()).hexdigest()
        with self.store.db:
            self.store.db.execute('INSERT INTO openclaw_connections VALUES(?,?,?,?,?,?,?,?,?)',
                                  (ident,user,name.strip(),transport,url,agent,secret,None,'unverified'))
        return {'connectionId':ident,**({'pairingToken':pair} if pair else {})}

    def delete(self, user, ident):
        if not self.connection(ident,user):raise ValueError('连接不存在或不属于当前用户')
        # References must be removed first, including the hidden template/other profiles.
        configs=[self.store.settings()]+[self.store.settings(u) for u in self.store.channel_overrides()]
        if any(c.get('connectionId')==ident for cfg in configs for c in cfg['channels']):raise ValueError('请先解除频道绑定，再删除连接')
        if self.store.db.execute('SELECT 1 FROM tasks WHERE connection_id=? AND state IN ('+','.join('?' for _ in LIVE)+') LIMIT 1',(ident,*LIVE)).fetchone():
            raise ValueError('该连接还有执行中的任务')
        with self.store.db:self.store.db.execute('DELETE FROM openclaw_connections WHERE id=?',(ident,))

    def check_routes(self, channels, user=None):
        for c in self.store.validate_channels(channels):
            if c['mode'] in EXTERNAL_AGENT_LABELS:
                if not user:raise ValueError('外部 Agent 需要在个人账户内绑定')
                if not self.connection(c['connectionId'],user,c['mode']):raise ValueError('外部 Agent 连接与此频道不匹配')

    def token(self, c):
        raw=bytes.fromhex(c['secret'])
        return self.store._cipher().decrypt(raw[:12],raw[12:],c['id'].encode()).decode()

    def session(self, seconds):
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                    timeout=aiohttp.ClientTimeout(total=seconds,connect=8,sock_read=None),trust_env=False)

    def headers(self, c):return {'Authorization':'Bearer '+self.token(c),'x-openclaw-agent-id':c['agent']}

    def available(self, task):
        if task['engine'] not in EXTERNAL_AGENT_LABELS:return True
        c=self.connection(task['connection_id'],task['user_id'],task['engine'])
        return not c or c['transport']=='https' or bool(c['status']=='ready' and c['seen'] and time.time()-c['seen']<30)

    async def probe(self, user, ident):
        c=self.connection(ident,user)
        if not c:raise ValueError('连接不存在或不属于当前用户')
        if c['transport']!='https':
            return {'ok':bool(c['seen'] and time.time()-c['seen']<30 and c['status']=='ready'),'status':next(x['status'] for x in self.public() if x['id']==ident)}
        status='unreachable'
        try:
            async with self.session(6) as client:
                async with client.get(c['url']+'/v1/models',headers=self.headers(c),allow_redirects=False) as r:
                    status={401:'unauthorized',403:'forbidden',404:'endpoint_disabled'}.get(r.status,'http_error')
                    if r.status==200:
                        data=json.loads(await r.content.read(262145))
                        if isinstance(data.get('data'),list):status='ready'
        except Exception:pass
        with self.store.db:self.store.db.execute('UPDATE openclaw_connections SET seen=?,status=? WHERE id=?',(time.time(),status,ident))
        return {'ok':status=='ready','status':status}

    async def run(self, task):
        ident=task['id'];c=self.connection(task['connection_id'],task['user_id'],task['engine'])
        label=EXTERNAL_AGENT_LABELS[task['engine']]
        if not c:
            await self.hub.change(ident,state='failed',summary=f'绑定的 {label} 连接已不可用');return
        timeout=self.store.settings(task['user_id'])['maxTaskSeconds']
        if c['transport']!='https':
            deadline=time.monotonic()+timeout
            await self.hub.change(ident,state='submitting',summary=f'任务已保存，等待 {label} 连接器接收')
            while self.store.task(ident)['state'] not in TERMINAL:
                current=self.store.task(ident);c=self.connection(c['id'])
                if time.monotonic()>deadline:
                    await self.hub.change(ident,state='delivery_unknown' if current['state']=='running' else 'failed',summary=f'等待 {label} 超时，请核对执行状态');return
                if current['state']=='running' and (not c or not c['seen'] or time.time()-c['seen']>45):
                    await self.hub.change(ident,state='delivery_unknown',summary=f'连接器已离线；任务未重新派发，请核对 {label}');return
                if current['state']=='submitting' and (not c or not c['seen'] or time.time()-c['seen']>30):
                    await self.hub.change(ident,state='queued',summary='连接器在接收前离线，等待重新上线');return
                await asyncio.sleep(.3)
            return
        submitted=False
        try:
            await self.hub.change(ident,state='submitting',summary='正在提交到 '+c['name'])
            if self.store.task(ident)['state']!='submitting':return
            headers={**self.headers(c),'x-openclaw-session-key':task['thread_id']}
            async with self.session(timeout) as client:
                submitted=True
                async with client.post(c['url']+'/v1/responses',headers=headers,allow_redirects=False,
                      json={'model':'openclaw','input':task['prompt'],'stream':True}) as r:
                    if r.status!=200:
                        safe={401:'Gateway 凭据无效',403:'Gateway 拒绝访问',404:'请启用 OpenClaw Responses API',409:'OpenClaw 会话已归档'}.get(r.status)
                        await self.hub.change(ident,state='failed' if safe else 'delivery_unknown',summary=safe or 'OpenClaw 返回异常；请核对执行状态');return
                    await self.hub.change(ident,state='running',summary='OpenClaw 正在执行；工具确认请前往 OpenClaw')
                    result='';last=0
                    async for raw in r.content:
                        if len(raw)>262144:raise ValueError('Oversized SSE line')
                        if not raw.startswith(b'data:'):continue
                        payload=raw[5:].strip()
                        if payload==b'[DONE]':break
                        event=json.loads(payload);kind=event.get('type','')
                        if kind=='response.output_text.delta':
                            result=(result+str(event.get('delta','')))[-64000:]
                            if time.monotonic()-last>1:self.store.update(ident,result=result);last=time.monotonic()
                        elif kind in ('response.completed','response.failed','response.incomplete'):
                            response=event.get('response') or {}
                            final='\n'.join(part.get('text','') for item in response.get('output',[]) if item.get('type')=='message' for part in item.get('content',[]) if part.get('type')=='output_text')
                            result=(final or result)[-64000:]
                            usage=response.get('usage') or {}
                            self.store.set_usage(ident,{k:usage.get(v) for k,v in [('inputTokens','input_tokens'),('outputTokens','output_tokens'),('totalTokens','total_tokens')]})
                            ok=kind=='response.completed' and response.get('status')=='completed'
                            await self.hub.change(ident,state='completed' if ok else 'failed',result=result,
                                 summary=(' '.join(result.split())[:180] or 'OpenClaw 任务完成') if ok else 'OpenClaw 执行未完成，请查看其控制台')
                            return
            await self.hub.change(ident,state='delivery_unknown',result=result,summary='OpenClaw 连接结束但未收到完成事件，请核对结果')
        except asyncio.CancelledError:raise
        except Exception:
            if self.store.task(ident)['state'] not in TERMINAL:
                await self.hub.change(ident,state='delivery_unknown' if submitted else 'failed',summary='OpenClaw 连接未确认，请核对实例状态；未自动重试')

    async def relay(self, body):
        engine=body.get('relayEngine','openclaw')
        if engine not in EXTERNAL_AGENT_LABELS:raise ValueError('连接类型无效')
        c=self.connection(body.get('connectionId'),engine=engine)
        token=body.get('relayToken','')
        if not c or c['transport']=='https' or not isinstance(token,str) or not hmac.compare_digest(c['secret'],hashlib.sha256(token.encode()).hexdigest()):
            raise ValueError('连接凭据无效或已撤销')
        label=EXTERNAL_AGENT_LABELS[engine]
        status=body.get('status')
        if status not in ('ready','unauthorized','endpoint_disabled','unreachable'):raise ValueError('连接状态无效')
        with self.store.db:self.store.db.execute('UPDATE openclaw_connections SET seen=?,status=? WHERE id=?',(time.time(),status,c['id']))
        if body.get('op')=='result':
            task=self.store.task(body.get('taskId'),c['user_id'])
            if task and task['engine']==engine and task['connection_id']==c['id'] and task['state'] in ('completed','failed','delivery_unknown') and task['state']==body.get('state') and task['result']==body.get('result'):
                return {'ok':True}  # Result retries acknowledge, never re-execute.
            if not task or task['engine']!=engine or task['connection_id']!=c['id'] or task['state'] not in ('running','delivery_unknown','interrupted'):
                raise ValueError('任务没有分配到此连接，或已结束')
            state=body.get('state');text=body.get('result','')
            if state not in ('completed','failed','delivery_unknown') or not isinstance(text,str) or len(text)>64000:raise ValueError('结果无效')
            self.store.set_usage(task['id'],body.get('usage'))
            await self.hub.change(task['id'],state=state,result=text,summary=(' '.join(text.split())[:180] or f'{label} 任务完成') if state=='completed' else f'{label} 执行未完成；请在主机核对')
            return {'ok':True}
        if body.get('op')!='poll':raise ValueError('连接器操作无效')
        if engine=='muse' and body.get('busy'):
            task=self.store.task(body.get('taskId'),c['user_id'])
            if task and task['engine']==engine and task['connection_id']==c['id'] and task['state'] in TERMINAL:
                return {'job':None,'stop':task['id']}
        # One claim per connector, persisted before returning any prompt.
        tasks=self.store.list_tasks(c['user_id'],limit=200)
        if status=='ready' and not body.get('busy') and not any(t['connection_id']==c['id'] and t['state']=='running' for t in tasks):
            for t in reversed(tasks):
                if t['engine']==engine and t['connection_id']==c['id'] and t['state']=='submitting' and t['id'] in self.hub.active:
                    await self.hub.change(t['id'],state='running',summary=f'{label} 正在执行')
                    return {'job':{'id':t['id'],'session':t['thread_id'],'text':t['prompt'],'agent':c['agent'],
                                   'timeout':self.store.settings(c['user_id'])['maxTaskSeconds'],
                                   **({'resume':t['thread_id']!='voxi-'+t['user_id']+'-'+t['id']} if engine=='muse' else {})}}
        return {'job':None}
