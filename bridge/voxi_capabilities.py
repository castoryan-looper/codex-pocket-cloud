"""Owner-scoped Skill imports and the supported remote MCP connector."""
import asyncio
from contextlib import asynccontextmanager
import hmac
from ipaddress import ip_address
import json
from pathlib import PurePosixPath
import re
import secrets
import time
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from aiohttp import web
from cryptography.exceptions import InvalidTag

from voxi_channel_tools import channel_tools_catalog, GITHUB_ID, IMPORTED_SKILL_ID

REGISTRY = 'https://registry.modelcontextprotocol.io/v0.1/servers'
GITHUB_API = 'https://api.github.com'
GITHUB_MCP = 'https://api.githubcopilot.com/mcp/readonly'
GITHUB_ENTRY = 'io.github.github/github-mcp-server'
GITHUB_TOOLS = ('get_file_contents', 'pull_request_read')
PROTOCOL = '2025-03-26'
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
FILE_PART = re.compile(r'[A-Za-z0-9_.-]{1,120}')
COMMIT = re.compile(r'[a-f0-9]{40}')
STATIC_EXTENSIONS = {'.md', '.txt', '.json', '.yaml', '.yml', '.csv'}
SKILL_STARTER = 'https://skills.sh/wshobson/agents/code-review-excellence'


def file_part(value):
    return value not in ('.','..') and FILE_PART.fullmatch(value)


def skill_source(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('请粘贴 skills.sh 技能页或 GitHub 的 SKILL.md 链接')
    try:
        url = urlsplit(value)
    except ValueError:
        raise ValueError('Skill 链接格式无效') from None
    if url.scheme != 'https' or url.query or url.fragment:
        raise ValueError('请使用完整 HTTPS Skill 链接')
    parts = unquote(url.path).strip('/').split('/')
    if len(parts) < 3 or any(not file_part(part) for part in parts) or not all(SLUG.fullmatch(part) for part in parts[:2]):
        raise ValueError('Skill 链接路径无效')
    owner, repo = parts[:2]
    if url.netloc in ('skills.sh', 'www.skills.sh') and len(parts) == 3:
        return owner, repo, 'HEAD', None, parts[2]
    if url.netloc == 'github.com' and len(parts) >= 5 and parts[2] == 'blob' and parts[-1] == 'SKILL.md':
        return owner, repo, parts[3], '/'.join(parts[4:]), None
    raise ValueError('支持 skills.sh 技能页或 GitHub 的 SKILL.md 文件链接')


async def read_bytes(response, limit):
    content = bytearray()
    async for chunk in response.content.iter_chunked(8192):
        content.extend(chunk)
        if len(content) > limit:
            raise ValueError('远端内容超过限制')
    return bytes(content)


def checked(response):
    if not 200 <= response.status < 300:
        reason = {401:'凭据无效或已撤销',403:'权限不足或达到服务限额',404:'资源不存在或当前授权无权访问',
                  429:'请求过于频繁，请稍后再试'}.get(response.status, '远端服务未完成请求')
        raise ValueError(f'{reason}（HTTP {response.status}）')


async def fetch(client, url, *, binary=False, limit=2000000, **kwargs):
    async with client.get(url, allow_redirects=False, **kwargs) as response:
        checked(response)
        raw = await read_bytes(response, limit)
    if binary:
        return raw.decode('utf-8-sig')
    return json.loads(raw)


async def import_skill(client, value):
    owner, repo, ref, path, slug = skill_source(value)
    base = f'{GITHUB_API}/repos/{owner}/{repo}'
    if COMMIT.fullmatch(ref):
        commit = ref
    else:
        reply = await fetch(client, base + '/commits/' + quote(ref, safe=''))
        commit = reply['sha']
    if not COMMIT.fullmatch(commit):
        raise ValueError('无法确定 Skill 的 GitHub 版本')
    tree = await fetch(client, f'{base}/git/trees/{commit}?recursive=1', limit=5000000)
    if tree.get('truncated'):
        raise ValueError('仓库目录过大，无法完整核对 Skill 文件')
    files = {item['path']: item['size'] for item in tree['tree'] if item.get('type') == 'blob'
             and item.get('mode') in ('100644','100755') and type(item.get('size')) is int}
    if slug:
        candidates = [file for file in files if file.split('/')[-2:] == [slug, 'SKILL.md']]
        if len(candidates) != 1:
            raise ValueError('找不到唯一匹配的 Skill，请改用具体 SKILL.md 链接')
        path = candidates[0]
    folder = str(PurePosixPath(path).parent)
    prefix = '' if folder == '.' else folder + '/'
    static = [file for file in files if file.startswith(prefix) and
              file[len(prefix):].split('/')[0] in ('references', 'assets') and
              PurePosixPath(file).suffix.lower() in STATIC_EXTENSIONS]
    names = [path, *sorted(static)]
    if path not in files or len(names) > 9 or sum(files.get(file, 0) for file in names) > 48000:
        raise ValueError('Skill 或静态资料超出首版导入范围')
    raw_base = f'https://raw.githubusercontent.com/{owner}/{repo}/{commit}/'
    for file in names:
        if any(not file_part(part) for part in file.split('/')):
            raise ValueError('Skill 文件路径包含不支持的字符')
    blobs = await asyncio.gather(*(fetch(client, raw_base + quote(file, safe='/'), binary=True,
                                         limit=48000) for file in names))
    if sum(len(blob.encode('utf-8')) for blob in blobs) > 48000:
        raise ValueError('Skill 或静态资料超出首版导入范围')
    match = re.match(r'\A---\s*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z', blobs[0], re.S)
    if not match or not match[2].strip():
        raise ValueError('SKILL.md 缺少有效的头部或说明')
    def field(name):
        line = re.search(r'^'+name+r':[ \t]*(.*)$', match[1], re.M)
        return line[1].strip().strip('"\'') if line else ''
    name = field('name')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('SKILL.md 的名称无效')
    description = field('description')
    if description in ('>', '|', '>-', '|-') or not description:
        description = next((line.strip() for line in match[2].splitlines() if line.strip() and not line.startswith('#')), name)
    source = f'https://github.com/{owner}/{repo}/blob/{commit}/{quote(path, safe="/")}'
    return {'name':name,'description':description[:400],'source':source,'commit':commit,
            'publisher':owner,'license':field('license')[:100],
            'instructions':match[2].strip(),'references':dict(zip((file[len(prefix):] for file in names[1:]), blobs[1:]))}


async def github_message(client, headers, method, params, ident=None):
    body = {'jsonrpc':'2.0','method':method,'params':params}
    if ident is not None:
        body['id'] = ident
    async with client.post(GITHUB_MCP, headers=headers, json=body, allow_redirects=False) as response:
        checked(response)
        session = response.headers.get('Mcp-Session-Id')
        if ident is None:
            return None, session
        if response.content_type == 'text/event-stream':
            consumed = 0
            async for line in response.content:
                consumed += len(line)
                if consumed > 800000:
                    raise ValueError('MCP 回复超过限制')
                if line.startswith(b'data:'):
                    value = json.loads(line[5:])
                    if value.get('id') == ident:
                        break
            else:
                raise ValueError('MCP 未返回完整结果')
        else:
            value = json.loads(await read_bytes(response, 800000))
        if value.get('id') != ident or not isinstance(value.get('result'), dict):
            raise ValueError('GitHub MCP 工具调用失败，请核对权限、仓库和参数')
        return value['result'], session


@asynccontextmanager
async def github_session(client, key):
    headers = {'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream',
               'X-MCP-Tools':','.join(GITHUB_TOOLS)}
    result, session = await github_message(client, headers, 'initialize',
       {'protocolVersion':PROTOCOL,'capabilities':{},'clientInfo':{'name':'voxi','version':'1.0'}}, 1)
    version = result.get('protocolVersion')
    if version not in (PROTOCOL,'2024-11-05','2025-06-18','2025-11-25'):
        raise ValueError('GitHub MCP 协议版本不受支持')
    if session:
        headers['Mcp-Session-Id'] = session
    headers['MCP-Protocol-Version'] = version
    await github_message(client, headers, 'notifications/initialized', {})
    try:
        yield headers
    finally:
        if session:
            try:
                async with asyncio.timeout(1), client.delete(GITHUB_MCP,headers=headers,allow_redirects=False):pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass


class CapabilityLibrary:
    def __init__(self, store):
        self.store = store
        self.store.db.executescript('''
        CREATE TABLE IF NOT EXISTS capability_skills(
          user_id TEXT NOT NULL, id TEXT NOT NULL, record TEXT NOT NULL, PRIMARY KEY(user_id,id));
        CREATE TABLE IF NOT EXISTS capability_github(
          user_id TEXT PRIMARY KEY, repository TEXT NOT NULL, secret BLOB NOT NULL,
          revision TEXT NOT NULL, tools TEXT NOT NULL, tested REAL);
        ''')
        self.checking = set()
        self.grants = {}
        self.grant_key = secrets.token_bytes(32)
        self.port = 8787

    @staticmethod
    def client(seconds=12):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds, connect=5),
                                     trust_env=False, read_bufsize=1024*1024)

    def skills(self, user):
        return {row['id']:json.loads(row['record']) for row in self.store.db.execute(
            'SELECT id,record FROM capability_skills WHERE user_id=? ORDER BY id',(user,))}

    def github(self, user):
        return self.store.db.execute('SELECT * FROM capability_github WHERE user_id=?',(user,)).fetchone()

    def catalog(self, user):
        catalog = channel_tools_catalog()
        catalog['skills'] += [{'id':ident,'name':item['name'],'description':item['description']}
                              for ident,item in self.skills(user).items()]
        github = self.github(user)
        if github and github['tested']:
            catalog['mcp'].append({'id':GITHUB_ID,'name':'GitHub 只读',
                                   'description':'仅限 '+github['repository']+' 的文件和 PR'})
        return catalog

    def snapshot(self, user):
        github = self.github(user)
        return {'starter':SKILL_STARTER,
                'skills':[{'id':ident, **{k:item[k] for k in ('name','description','source','commit','publisher')}}
                          for ident,item in self.skills(user).items()],
                'github':{'repository':github['repository'],'tested':github['tested']} if github else None}

    def check_routes(self, routes, user):
        owned = self.skills(user) if user else {}
        for route in routes:
            imported = [ident for ident in route.get('skills', []) if IMPORTED_SKILL_ID.fullmatch(ident)]
            if any(ident not in owned for ident in imported):
                raise ValueError('请选择当前账户已导入的 Skill')
            if len(imported)>3 or sum(len(owned[ident]['instructions'])+sum(map(len,owned[ident]['references'].values()))
                                     for ident in imported)>64000:
                raise ValueError('每个频道最多启用 3 个导入的 Skill，合计不超过 64000 字符')
            if GITHUB_ID in route.get('mcp', []):
                github = self.github(user)
                if not github or not github['tested']:
                    raise ValueError('请先授权 GitHub MCP 并通过只读试跑')

    def credentials(self, user, row):
        raw = row['secret']
        try:
            return self.store._cipher().decrypt(raw[:12], raw[12:], ('github:'+user).encode()).decode()
        except (ValueError, OSError, InvalidTag):
            raise ValueError('GitHub 授权无法读取，请重新授权') from None

    def runtime_signature(self, user, selected):
        if GITHUB_ID not in selected:
            return ','.join(selected)
        row = self.github(user)
        if not row or not row['tested']:
            raise ValueError('GitHub MCP 尚未通过试跑或授权已撤销')
        return ','.join(selected)+':'+row['revision']

    def grant(self, user):
        row = self.github(user)
        revision = row['revision']
        # App-server can retain a resumed thread's existing MCP connection.
        token = hmac.digest(self.grant_key,(user+':'+revision).encode(),'sha256').hex()
        count = self.grants[token][2] if token in self.grants else 0
        self.grants[token] = (user, revision, count+1)
        return token, {'url':f'http://127.0.0.1:{self.port}/voxi/capability-mcp',
                       'http_headers':{'X-Voxi-MCP-Grant':token},'enabled':True,'required':True,
                       'enabled_tools':list(GITHUB_TOOLS),'startup_timeout_sec':8,'tool_timeout_sec':30}

    def release(self, token):
        user, revision, count = self.grants[token]
        if count == 1:
            del self.grants[token]
        else:
            self.grants[token] = (user, revision, count-1)

    def detach_github(self, user):
        if user not in self.store.channel_overrides():
            return
        channels = self.store.settings(user)['channels']
        changed = False
        for route in channels:
            if GITHUB_ID in route.get('mcp', []):
                route['mcp'].remove(GITHUB_ID)
                changed = True
        if changed:
            self.store.db.execute('UPDATE config SET value=? WHERE key=?',
                                  (json.dumps(channels,ensure_ascii=False),'channels:'+user))

    def delete_user(self, user):
        with self.store.db:
            self.store.db.execute('DELETE FROM capability_github WHERE user_id=?',(user,))
            self.store.db.execute('DELETE FROM capability_skills WHERE user_id=?',(user,))

    async def manage(self, user, body):
        action = body['action']
        if action == 'capability_list':
            return self.snapshot(user)
        if user in self.checking or len(self.checking) >= 8:
            raise ValueError('有检测正在运行，请稍后再试')
        self.checking.add(user)
        try:
            return await self.change(user, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, AttributeError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError('外部服务暂不可达或返回格式无效') from None
        finally:
            self.checking.discard(user)

    async def change(self, user, body):
        action = body['action']
        if action == 'capability_registry':
            query = body.get('query','github-mcp-server')
            if not isinstance(query,str) or not 1 <= len(query.strip()) <= 80:
                raise ValueError('请输入 1–80 个字符搜索 MCP')
            async with self.client(8) as client:
                result = await fetch(client, REGISTRY, params={'search':query.strip(),'limit':'25','version':'latest'})
            servers = []
            for item in result['servers']:
                server = item['server']
                servers.append({'id':server['name'],'description':server.get('description','')[:300],
                                'version':server['version'],'remote':bool(server.get('remotes')),
                                'supported':server['name']==GITHUB_ENTRY})
            return {'servers':servers}
        if action in ('capability_skill_preview','capability_skill_add'):
            if action == 'capability_skill_add' and not COMMIT.fullmatch(body.get('commit') or ''):
                raise ValueError('请先预览 Skill 来源和版本')
            async with asyncio.timeout(16), self.client() as client:
                record = await import_skill(client, body.get('url'))
            if action.endswith('preview'):
                return {**record,'references':list(record['references'])}
            if record['commit'] != body['commit']:
                raise ValueError('Skill 版本已变化，请重新预览')
            existing = self.skills(user)
            if any(item['source']==record['source'] for item in existing.values()):
                return self.snapshot(user)
            if len(existing)>=12:
                raise ValueError('每个账户最多导入 12 个 Skill')
            with self.store.db:
                self.store.db.execute('INSERT INTO capability_skills VALUES(?,?,?)',
                                      (user,'skill-'+secrets.token_hex(16),json.dumps(record,ensure_ascii=False)))
        elif action == 'capability_skill_remove':
            ident = body.get('id')
            if not isinstance(ident,str) or ident not in self.skills(user):
                raise ValueError('Skill 不存在或不属于当前账户')
            if any(ident in route.get('skills',[]) for route in self.store.settings(user)['channels']):
                raise ValueError('请先解除频道绑定')
            with self.store.db:
                self.store.db.execute('DELETE FROM capability_skills WHERE user_id=? AND id=?',(user,ident))
        elif action == 'capability_github_connect':
            repository,key = body.get('repository'),body.get('token')
            if not isinstance(repository,str) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',repository) or len(repository)>150:
                raise ValueError('请填写 GitHub 仓库，格式为 owner/repo')
            if not isinstance(key,str) or not re.fullmatch(r'github_pat_[A-Za-z0-9_]{30,250}',key):
                raise ValueError('请填写 GitHub fine-grained personal access token')
            async with asyncio.timeout(16), self.client() as client:
                async with github_session(client,key) as headers:
                    result,_ = await github_message(client,headers,'tools/list',{},2)
            tools = [tool for tool in result['tools'] if tool.get('name') in GITHUB_TOOLS]
            if {tool['name'] for tool in tools} != set(GITHUB_TOOLS):
                raise ValueError('GitHub MCP 未返回所需的文件读取与 PR 工具')
            nonce = secrets.token_bytes(12)
            secret = nonce+self.store._cipher().encrypt(nonce,key.encode(),('github:'+user).encode())
            with self.store.db:
                self.detach_github(user)
                self.store.db.execute('INSERT OR REPLACE INTO capability_github VALUES(?,?,?,?,?,NULL)',
                                      (user,repository,secret,secrets.token_hex(16),json.dumps(tools)))
        elif action == 'capability_github_test':
            row = self.github(user)
            if not row:
                raise ValueError('请先授权 GitHub MCP')
            owner,repo = row['repository'].split('/')
            path = body.get('path') or 'README.md'
            number = body.get('pullNumber')
            if type(number) is not int or not 1 <= number <= 2**31-1:
                raise ValueError('请输入用于试跑的 Pull Request 编号')
            if not isinstance(path,str) or len(path)>240 or path.startswith('/') or any(
                    not file_part(part) for part in path.split('/')):
                raise ValueError('请输入仓库中的相对文件路径')
            async with asyncio.timeout(16), self.client() as client:
                async with github_session(client,self.credentials(user,row)) as headers:
                    result,_ = await github_message(client,headers,'tools/call',
                         {'name':'get_file_contents','arguments':{'owner':owner,'repo':repo,'path':path}},2)
                    if not github_result_ok(result):
                        raise ValueError('只读试跑未取得文件，请检查文件路径和 Contents 权限')
                    result,_ = await github_message(client,headers,'tools/call',
                         {'name':'pull_request_read','arguments':{'owner':owner,'repo':repo,'pullNumber':number,'method':'get_diff'}},3)
                    if not github_result_ok(result):
                        raise ValueError('只读试跑未取得 PR 差异，请检查 PR 编号和 Pull requests 权限')
            with self.store.db:
                self.store.db.execute('UPDATE capability_github SET tested=? WHERE user_id=? AND revision=?',
                                      (time.time(),user,row['revision']))
        elif action == 'capability_github_remove':
            with self.store.db:
                self.detach_github(user)
                self.store.db.execute('DELETE FROM capability_github WHERE user_id=?',(user,))
        else:
            raise ValueError('能力库操作无效')
        return self.snapshot(user)

    async def mcp(self, request):
        try:
            local = ip_address(request.remote or '').is_loopback
        except ValueError:
            local = False
        grant = self.grants.get(request.headers.get('X-Voxi-MCP-Grant','')) if local else None
        row = self.github(grant[0]) if grant else None
        if not row or not row['tested'] or row['revision']!=grant[1]:
            raise web.HTTPForbidden()
        if request.content_length is None or request.content_length > 16384:
            raise web.HTTPBadRequest()
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if not isinstance(body,dict):
            raise web.HTTPBadRequest()
        method, ident = body.get('method'), body.get('id')
        if method == 'notifications/initialized':
            return web.Response(status=202)
        if method == 'initialize':
            requested = (body.get('params') or {}).get('protocolVersion')
            protocol = requested if requested in (PROTOCOL,'2025-06-18','2025-11-25') else PROTOCOL
            result = {'protocolVersion':protocol,'capabilities':{'tools':{}},
                      'serverInfo':{'name':'voxi-github-readonly','version':'1.0'}}
        elif method == 'tools/list':
            result = {'tools':json.loads(row['tools'])}
        elif method == 'tools/call':
            params = body.get('params')
            args = params.get('arguments') if isinstance(params,dict) else None
            if not isinstance(args,dict) or params.get('name') not in GITHUB_TOOLS or (
                str(args.get('owner',''))+'/'+str(args.get('repo',''))).lower()!=row['repository'].lower():
                result = {'isError':True,'content':[{'type':'text','text':'仅允许读取已授权仓库的文件和 PR'}]}
            else:
                try:
                    async with asyncio.timeout(24), self.client() as client:
                        async with github_session(client,self.credentials(grant[0],row)) as headers:
                            result,_ = await github_message(client,headers,'tools/call',params,2)
                except (ValueError, TypeError, AttributeError, aiohttp.ClientError, asyncio.TimeoutError):
                    result = {'isError':True,'content':[{'type':'text','text':'GitHub MCP 调用失败，请核对授权和仓库'}]}
        elif method == 'ping':
            result = {}
        else:
            return web.json_response({'jsonrpc':'2.0','id':ident,'error':{'code':-32601,'message':'Unsupported method'}})
        return web.json_response({'jsonrpc':'2.0','id':ident,'result':result})


def github_result_ok(result):
    return result.get('isError') is not True and bool(result.get('content'))
