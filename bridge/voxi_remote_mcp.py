"""Registry-backed HTTPS MCP connections with owner and per-call authorization."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import time
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver
from cryptography.exceptions import InvalidTag

REGISTRY = 'https://registry.modelcontextprotocol.io/v0.1/servers'
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26')
REMOTE_ID = re.compile(r'mcp-[a-f0-9]{32}')
HEADER = re.compile(r'[A-Za-z0-9!#$%&\x27*+.^_\x60|~-]{1,80}')
VARIABLE = re.compile(r'[A-Za-z0-9_.-]{1,64}')
TOOL = re.compile(r'[A-Za-z0-9_.-]{1,128}')
RESERVED = {'host','content-type','accept','content-length','transfer-encoding','connection',
            'cookie','origin','forwarded','upgrade','user-agent','mcp-session-id','mcp-protocol-version'}
MAX_BYTES = 512 * 1024


def public_ip(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        addresses = await asyncio.get_running_loop().getaddrinfo(host,port,family=family,type=socket.SOCK_STREAM)
        if not addresses or any(not public_ip(row[4][0]) for row in addresses):
            raise OSError('MCP requires public addresses')
        return [{'hostname':host,'host':row[4][0],'port':port,'family':row[0],
                 'proto':row[2],'flags':socket.AI_NUMERICHOST} for row in addresses]

    async def close(self):
        pass


def public_url(value):
    if not isinstance(value,str) or len(value)>1500 or re.search(r'[\s\\{}]',value):
        raise ValueError('MCP 地址格式无效')
    try:
        url = urlsplit(value)
        valid = (url.scheme=='https' and url.hostname and not url.username and not url.password and
                 not url.fragment and url.port in (None,443) and
                 re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+',url.hostname))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('MCP 需要公开 HTTPS 域名及标准端口')
    # aiohttp may bypass its resolver when the host is an IP literal.
    if not re.search(r'[A-Za-z]',url.hostname.rsplit('.',1)[-1]):
        raise ValueError('请使用公开域名')
    return url


def field(kind,name,meta):
    pattern = HEADER if kind=='header' else VARIABLE
    if not isinstance(name,str) or not pattern.fullmatch(name) or not isinstance(meta,dict):
        raise ValueError('Registry 配置字段无效')
    if kind=='header' and (name.lower() in RESERVED or name.lower().startswith(('proxy-','x-forwarded-'))):
        raise ValueError('Registry 使用了保留请求头')
    choices = meta.get('choices',[])
    if not isinstance(choices,list) or len(choices)>30 or any(not isinstance(v,str) or len(v)>512 for v in choices):
        raise ValueError('Registry 配置选项无效')
    secret = bool(meta.get('isSecret')) or (kind=='header' and (
        name.lower()=='authorization' or any(word in name.lower() for word in ('key','token','secret','auth'))))
    return {'kind':kind,'name':name,'description':str(meta.get('description',''))[:300],
            'required':bool(meta.get('isRequired')),'secret':secret,'choices':choices,
            'default':str(meta.get('default',''))[:512] if not secret else ''}


def descriptors(server):
    result = []
    for index, remote in enumerate((server.get('remotes') or [])[:16]):
        if not isinstance(remote,dict) or remote.get('type')!='streamable-http':
            continue
        try:
            raw = remote['url']
            if not isinstance(raw,str) or len(raw)>1500:
                continue
            parts = urlsplit(raw)
            # User-supplied values are allowed in path/query only; the publisher fixes the host.
            if '{' in parts.netloc or '}' in parts.netloc:
                continue
            variables = remote.get('variables') or {}
            headers = remote.get('headers') or []
            if not isinstance(variables,dict) or not isinstance(headers,list) or len(variables)+len(headers)>20:
                continue
            fields = [field('variable',name,meta) for name,meta in variables.items()]
            if any(item['secret'] for item in fields):
                continue
            fields += [field('header',header.get('name'),header) for header in headers if isinstance(header,dict)]
            tokens = set(re.findall(r'\{([A-Za-z0-9_.-]+)\}',raw))
            if tokens != set(variables) or len({f['name'].lower() for f in fields if f['kind']=='header'}) != len(headers):
                continue
            url = public_url(re.sub(r'\{[A-Za-z0-9_.-]+\}','placeholder',raw))
            result.append({'index':index,'host':url.hostname,'template':raw,'fields':fields})
        except (ValueError, TypeError, KeyError):
            continue
    return result


def connection_parameters(entry, values):
    if not isinstance(values,dict) or set(values)-{f['kind']+':'+f['name'] for f in entry['fields']}:
        raise ValueError('接入配置字段无效')
    url = entry['template']
    headers = {}
    for f in entry['fields']:
        name = f['name']
        value = values.get(f['kind']+':'+name,f['default'])
        if (not isinstance(value,str) or len(value)>2048 or any(ord(c)<32 or ord(c)==127 for c in value)
                or f['required'] and not value or value and f['choices'] and value not in f['choices']):
            raise ValueError('请检查配置项 '+name)
        if f['kind']=='variable':
            if value in ('.','..'):
                raise ValueError('端点变量无效')
            url = url.replace('{'+name+'}',quote(value,safe=''))
        elif value:
            headers[name] = value
    resolved = public_url(url)
    if resolved.hostname!=entry['host']:
        raise ValueError('MCP 端点域名发生变化')
    return urlunsplit(resolved),headers


async def payload(response, ident=None):
    if response.status==401:
        raise ValueError('服务要求授权。请核对 Registry 中声明的 API Key；OAuth 服务需要后续接入。')
    if response.status not in (200,201,202,204):
        raise ValueError(f'远端 MCP 返回 HTTP {response.status}，请核对配置或服务状态')
    if ident is None:
        return None
    if response.content_type=='text/event-stream':
        lines, size = [], 0
        async for line in response.content:
            size += len(line)
            if size>MAX_BYTES:
                break
            if line.startswith(b'data:'):
                lines.append(line[5:].strip())
            elif not line.strip() and lines:
                data=json.loads(b'\n'.join(lines));lines.clear()
                if isinstance(data,dict) and data.get('id')==ident:
                    return data
        raise ValueError('远端 MCP 未返回完整结果')
    raw = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        raw.extend(chunk)
        if len(raw)>MAX_BYTES:
            raise ValueError('远端 MCP 响应超过限制')
    return json.loads(raw)


class RemoteSession:
    def __init__(self,client,url,headers):
        self.client,self.url = client,url
        self.headers = {**headers,'Accept':'application/json, text/event-stream'}
        self.counter = 0

    async def send(self,method,params,notification=False):
        self.counter += 1
        ident = None if notification else self.counter
        body = {'jsonrpc':'2.0','method':method,'params':params}
        if ident is not None:
            body['id'] = ident
        async with self.client.post(self.url,headers=self.headers,json=body,allow_redirects=False) as response:
            data = await payload(response,ident)
            if notification:
                return None
            if not isinstance(data,dict) or data.get('id')!=ident or not isinstance(data.get('result'),dict):
                raise ValueError('远端 MCP 未完成 '+method)
            if method=='initialize':
                session = response.headers.get('Mcp-Session-Id')
                if session:
                    self.headers['Mcp-Session-Id'] = session
            return data['result']

    async def tools(self):
        tools, cursor, seen = {}, None, set()
        for _ in range(8):
            page = await self.send('tools/list',{'cursor':cursor} if cursor else {})
            for item in page.get('tools',[]):
                if (not isinstance(item,dict) or not isinstance(item.get('name'),str) or
                        not TOOL.fullmatch(item['name']) or not isinstance(item.get('inputSchema'),dict)):
                    continue
                if len(tools)>=200:
                    raise ValueError('远端工具目录超过首版限制')
                tools[item['name']] = {key:item[key] for key in
                     ('name','title','description','inputSchema','outputSchema','annotations') if key in item}
            cursor=page.get('nextCursor')
            if not cursor:
                return list(tools.values())
            if not isinstance(cursor,str) or cursor in seen or len(cursor)>1000:
                break
            seen.add(cursor)
        raise ValueError('远端工具目录分页异常')


@asynccontextmanager
async def upstream(url,headers,seconds=12):
    public_url(url)
    async with asyncio.timeout(seconds), aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(),use_dns_cache=False),
        timeout=aiohttp.ClientTimeout(total=seconds,connect=5),trust_env=False,read_bufsize=MAX_BYTES,
    ) as client:
        conn = RemoteSession(client,url,headers)
        info = await conn.send('initialize',{'protocolVersion':PROTOCOLS[0],'capabilities':{},
                                              'clientInfo':{'name':'voxi','version':'1.0'}})
        version=info.get('protocolVersion')
        if version not in PROTOCOLS:
            raise ValueError('远端 MCP 协议版本暂未支持')
        conn.headers['MCP-Protocol-Version']=version
        await conn.send('notifications/initialized',{},notification=True)
        try:
            yield conn
        finally:
            if 'Mcp-Session-Id' in conn.headers:
                try:
                    async with asyncio.timeout(1), client.delete(url,headers=conn.headers,allow_redirects=False):pass
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass


class RemoteLibrary:
    def __init__(self,store):
        self.store=store
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS capability_remote(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,server TEXT NOT NULL,version TEXT NOT NULL,
          host TEXT NOT NULL,secret BLOB NOT NULL,tools TEXT NOT NULL,
          selected TEXT NOT NULL,tested REAL)''')
        self.port=8787
        self.approve=None
        self.live=None
        self.grants={}
        self.grant_key=secrets.token_bytes(32)

    def get(self,user,ident):
        if not isinstance(ident,str) or not REMOTE_ID.fullmatch(ident):
            return None
        return self.store.db.execute('SELECT * FROM capability_remote WHERE user_id=? AND id=?',(user,ident)).fetchone()

    def list(self,user):
        return [{'id':r['id'],'server':r['server'],'version':r['version'],'host':r['host'],'tested':r['tested'],
                 'selected':json.loads(r['selected']),
                 'tools':[{'name':t['name']} for t in json.loads(r['tools'])]}
                for r in self.store.db.execute('SELECT * FROM capability_remote WHERE user_id=? ORDER BY server,id',(user,))]

    def catalog(self,user):
        return [{'id':r['id'],'name':r['server'],'description':'远程 MCP · '+r['host']+' · 调用前需确认'}
                for r in self.store.db.execute('SELECT id,server,host FROM capability_remote WHERE user_id=? AND tested IS NOT NULL',(user,))]

    def validate(self,user,ident):
        row=self.get(user,ident)
        if not row or not row['tested']:
            raise ValueError('请选择当前账户已通过试跑的远程 MCP')
        return row

    def signature(self,row):
        return hashlib.sha256(row['selected'].encode()).hexdigest()[:24]

    def connection(self,row):
        raw=row['secret']
        try:
            data=self.store._cipher().decrypt(raw[:12],raw[12:],row['id'].encode())
        except (OSError,ValueError,InvalidTag):
            raise ValueError('MCP 授权无法读取，请重新接入') from None
        return json.loads(data)

    def grant(self,task,row):
        scope=(task['user_id'],row['id'],str(task['channel']),self.signature(row))
        token=hmac.digest(self.grant_key,'|'.join(scope).encode(),'sha256').hex()
        self.grants[token]=(task['user_id'],row['id'],task['id'],scope[-1])
        return token,{'url':f'http://127.0.0.1:{self.port}/voxi/remote-mcp',
                      'http_headers':{'X-Voxi-MCP-Grant':token},'enabled':True,'required':True,
                      'enabled_tools':json.loads(row['selected']),'startup_timeout_sec':8,'tool_timeout_sec':130}

    async def entry(self,name,version):
        if (not isinstance(name,str) or not isinstance(version,str) or len(name)>200 or len(version)>100
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',name) or name=='io.github.github/github-mcp-server'
                or not re.fullmatch(r'[A-Za-z0-9_.+-]+',version) or version in ('.','..','latest')):
            raise ValueError('请选择 Registry 中的确定版本')
        url=REGISTRY+'/'+quote(name,safe='')+'/versions/'+quote(version,safe='')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6),trust_env=False) as client:
            async with client.get(url,allow_redirects=False) as response:
                data=await payload(response,0)
        item=data['server']
        if (item['name'],item['version'])!=(name,version) or data.get('_meta',{}).get(
                'io.modelcontextprotocol.registry/official',{}).get('status','active')!='active':
            raise ValueError('此 Registry 版本不可接入')
        return descriptors(item)

    async def manage(self,user,body):
        action=body['action']
        if action in ('capability_remote_details','capability_remote_connect'):
            name,version=body.get('server'),body.get('version')
            entries=await self.entry(name,version)
            if not entries:
                raise ValueError('该版本没有可用的公开 Streamable HTTP 配置')
            if action.endswith('details'):
                return {'entries':entries}
            index=body.get('index')
            entry=next((e for e in entries if type(index) is int and e['index']==index),None)
            if not entry or len(self.list(user))>=12:
                raise ValueError('接入点无效或已达到每账户 12 个远程 MCP 上限')
            url,headers=connection_parameters(entry,body.get('values'))
            async with upstream(url,headers,9) as conn:
                tools=await conn.tools()
            if not tools:
                raise ValueError('远端 MCP 没有可用工具')
            packed=json.dumps(tools,ensure_ascii=False)
            if len(packed.encode())>MAX_BYTES:
                raise ValueError('远端工具目录超过限制')
            ident=secrets.token_hex(16)
            ident='mcp-'+ident
            nonce=secrets.token_bytes(12)
            secret=nonce+self.store._cipher().encrypt(nonce,json.dumps([url,headers]).encode(),ident.encode())
            with self.store.db:
                self.store.db.execute('INSERT INTO capability_remote VALUES(?,?,?,?,?,?,?,?,NULL)',
                    (ident,user,name,version,entry['host'],secret,packed,'[]'))
            return {'id':ident}

        ident=body.get('id')
        row=self.get(user,ident)
        if not row:
            raise ValueError('远程 MCP 不存在或不属于当前账户')
        if action=='capability_remote_schema':
            tool=next((t for t in json.loads(row['tools']) if t['name']==body.get('tool')),None)
            if not tool:
                raise ValueError('找不到该工具')
            return {'schema':tool['inputSchema'],'description':str(tool.get('description',''))[:1000]}
        if action=='capability_remote_remove':
            with self.store.db:
                self.store.db.execute('DELETE FROM capability_remote WHERE id=? AND user_id=?',(ident,user))
                self.detach(user,ident)
            return {'id':ident}
        if action!='capability_remote_test':
            raise ValueError('远程 MCP 操作无效')
        tools={t['name'] for t in json.loads(row['tools'])}
        selected,name,args=body.get('selected'),body.get('tool'),body.get('arguments')
        if (not isinstance(selected,list) or not 1<=len(selected)<=24 or
                any(not isinstance(t,str) or t not in tools for t in selected) or len(set(selected))!=len(selected)
                or name not in selected or not isinstance(args,dict) or len(json.dumps(args))>16000):
            raise ValueError('请勾选工具，并填写试跑工具及 JSON 对象参数')
        url,headers=self.connection(row)
        async with upstream(url,headers,12) as conn:
            result=await conn.send('tools/call',{'name':name,'arguments':args})
        if not result_ok(result):
            raise ValueError('远端工具没有返回成功结果，请核对授权和参数')
        with self.store.db:
            self.store.db.execute('UPDATE capability_remote SET selected=?,tested=? WHERE id=? AND user_id=?',
                                  (json.dumps(sorted(selected)),time.time(),ident,user))
        return {'id':ident,'preview':json.dumps(result,ensure_ascii=False)[:3500]}

    def detach(self,user,ident):
        channels=self.store.settings(user)['channels']
        changed=False
        for route in channels:
            if ident in route.get('mcp',[]):
                route['mcp'].remove(ident)
                changed=True
        if changed:
            self.store.db.execute('UPDATE config SET value=? WHERE key=?',
                                  (json.dumps(channels,ensure_ascii=False),'channels:'+user))

    async def mcp(self,request):
        if request.remote not in ('127.0.0.1','::1'):
            raise web.HTTPForbidden()
        grant=self.grants.get(request.headers.get('X-Voxi-MCP-Grant',''))
        row=self.get(grant[0],grant[1]) if grant else None
        if not row or not row['tested'] or self.signature(row)!=grant[3]:
            raise web.HTTPForbidden()
        if request.content_length is None or request.content_length>32768:
            raise web.HTTPBadRequest()
        try:
            body=await request.json()
            method,ident=body['method'],body.get('id')
            params=body.get('params') or {}
            if not isinstance(params,dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPBadRequest() from None
        if method=='notifications/initialized':
            return web.Response(status=202)
        if method=='initialize':
            requested=params.get('protocolVersion')
            result={'protocolVersion':requested if requested in PROTOCOLS else PROTOCOLS[-1],
                    'capabilities':{'tools':{}},'serverInfo':{'name':'voxi-remote-mcp','version':'1.0'}}
        elif method=='tools/list':
            selected=json.loads(row['selected'])
            result={'tools':[t for t in json.loads(row['tools']) if t['name'] in selected]}
        elif method=='ping':
            result={}
        elif method=='tools/call':
            result=await self.tool_call(grant,row,params)
        else:
            return web.json_response({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Unsupported method'}})
        return web.json_response({'jsonrpc':'2.0','id':ident,'result':result})

    async def tool_call(self,grant,row,params):
        name,args=params.get('name'),params.get('arguments',{})
        if name not in json.loads(row['selected']) or not isinstance(args,dict) or len(json.dumps(args))>16000:
            return tool_error('工具未启用或参数无效')
        if not self.approve or not await self.approve(grant[2],row['server']+' · '+row['host'],name,args):
            return tool_error('用户未批准本次远程调用')
        current=self.get(grant[0],row['id'])
        if not current or self.signature(current)!=grant[3] or not current['tested']:
            return tool_error('MCP 授权已变更，请重新发起任务')
        try:
            url,headers=self.connection(current)
            async with upstream(url,headers,24) as conn:
                latest=self.get(grant[0],row['id'])
                if not latest or self.signature(latest)!=grant[3] or not self.live or not self.live(grant[2]):
                    return tool_error('任务或 MCP 授权已失效')
                return await conn.send('tools/call',{'name':name,'arguments':args})
        except (ValueError,TypeError,KeyError,aiohttp.ClientError,asyncio.TimeoutError):
            return tool_error('远程 MCP 调用失败，请检查授权、参数和服务状态')


def tool_error(message):
    return {'isError':True,'content':[{'type':'text','text':message}]}


def result_ok(result):
    return result.get('isError') is not True and bool(result.get('content') or result.get('structuredContent'))
