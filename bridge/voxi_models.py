"""User-authorized model connections. Each provider controls its own billing."""
import asyncio
import json
import re
import secrets

import aiohttp
from cryptography.exceptions import InvalidTag

from voxi_agent_store import LIVE


PROVIDERS = {
    'deepseek': {'name': 'DeepSeek', 'defaultModel': 'deepseek-flash', 'workspaceRequired': False,
                 'docs': 'https://api-docs.deepseek.com/'},
    'qwen': {'name': '千问 · 阿里云百炼（北京）', 'defaultModel': 'qwen-plus', 'workspaceRequired': True,
             'docs': 'https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen'},
}


class ModelConnections:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store
        self.checking = set()

    def connection(self, user, ident):
        if not isinstance(ident, str):
            return None
        row = self.store.db.execute('SELECT * FROM model_connections WHERE user_id=? AND id=?', (user, ident)).fetchone()
        return dict(row) if row else None

    def public(self, user=None):
        query = 'SELECT id,user_id,name,provider,model,workspace FROM model_connections'
        rows = self.store.db.execute(query + ' WHERE user_id=? ORDER BY name', (user,)) if user else self.store.db.execute(query + ' ORDER BY name')
        return [dict(row) for row in rows]

    async def create(self, user, body):
        provider, name, model, workspace, key = (body.get(k, '') for k in ('provider', 'name', 'model', 'workspace', 'key'))
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError('请选择支持的模型服务')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
            raise ValueError('连接名称需要 1–64 个字符')
        if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,128}', model):
            raise ValueError('请填写有效的模型 ID')
        if provider == 'qwen':
            if not isinstance(workspace, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', workspace) or workspace.endswith('-'):
                raise ValueError('请填写百炼北京地域的业务空间 ID')
        else:
            workspace = ''
        ident = body.get('connectionId')
        existing = None
        if ident is not None:
            if not isinstance(ident, str):
                raise ValueError('模型连接编号无效')
            existing = self.connection(user, ident)
            if not existing:
                raise ValueError('模型连接不存在或不属于当前用户')
            if (provider, model, workspace) != (existing['provider'], existing['model'], existing['workspace']):
                raise ValueError('变更模型或业务空间时请添加新的接入')
            if key == '':
                key = self.token(existing)
        if not isinstance(key, str) or not re.fullmatch(r'[!-~]{12,512}', key):
            raise ValueError('请填写有效的 API Key')
        if not existing and len(self.public(user)) >= 16:
            raise ValueError('每个账户最多 16 个模型接入')
        ident = ident or secrets.token_hex(16)
        record = {'provider': provider, 'model': model, 'workspace': workspace}
        await self.check(user, record, key)
        nonce = secrets.token_bytes(12)
        secret = nonce + self.store._cipher().encrypt(nonce, key.encode(), ident.encode())
        with self.store.db:
            if existing:
                updated = self.store.db.execute('UPDATE model_connections SET name=?,secret=? WHERE id=? AND user_id=?',
                                                (name.strip(), secret, ident, user))
                if not updated.rowcount:
                    raise ValueError('验证期间连接已删除')
            else:
                self.store.db.execute('INSERT INTO model_connections VALUES(?,?,?,?,?,?,?)',
                                      (ident, user, name.strip(), provider, model, workspace, secret))
        return {'connectionId': ident}

    def token(self, record):
        raw = record['secret']
        try:
            return self.store._cipher().decrypt(raw[:12], raw[12:], record['id'].encode()).decode()
        except (OSError, ValueError, InvalidTag):
            raise ValueError('模型凭据无法读取，请检查加密密钥或重新接入') from None

    async def check(self, user, record, key):
        if user in self.checking or len(self.checking) >= 8:
            raise ValueError('已有连接正在检测，请稍后重试')
        self.checking.add(user)
        try:
            await self.complete(record, key, 'Reply with OK.', seconds=9, max_tokens=64)
        finally:
            self.checking.discard(user)

    async def probe(self, user, ident):
        record = self.connection(user, ident)
        if not record:
            raise ValueError('模型连接不存在或不属于当前用户')
        await self.check(user, record, self.token(record))
        return {'ok': True}

    def delete(self, user, ident):
        if not self.connection(user, ident):
            raise ValueError('模型连接不存在或不属于当前用户')
        if any(c.get('connectionId') == ident for c in self.store.settings(user)['channels']):
            raise ValueError('请先解除频道绑定，再删除连接')
        marks = ','.join('?' for _ in LIVE)
        if self.store.db.execute(f'SELECT 1 FROM tasks WHERE connection_id=? AND state IN ({marks}) LIMIT 1',
                                 (ident, *LIVE)).fetchone():
            raise ValueError('该连接还有执行中的任务')
        with self.store.db:
            self.store.db.execute('DELETE FROM model_connections WHERE id=? AND user_id=?', (ident, user))

    async def complete(self, record, key, text, *, seconds=90, max_tokens=1800):
        if record['provider'] == 'deepseek':
            url = 'https://api.deepseek.com/chat/completions'
            options = {'thinking': {'type': 'disabled'}}
        else:
            url = f"https://{record['workspace']}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
            options = {'enable_thinking': False}
        body = {'model': record['model'], 'messages': [{'role': 'user', 'content': text}],
                'max_tokens': max_tokens, 'stream': False, **options}
        try:
            timeout = aiohttp.ClientTimeout(total=seconds, connect=min(5, seconds))
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
                async with client.post(url, headers={'Authorization': 'Bearer ' + key}, json=body, allow_redirects=False) as response:
                    if response.status != 200:
                        messages = {400: '请检查模型 ID 和接口兼容性', 401: '请检查 API Key', 403: '请检查模型权限',
                                    404: '请检查模型 ID 或业务空间', 429: '请检查调用额度或速率限制'}
                        raise ValueError(f'模型服务返回 HTTP {response.status}。' + messages.get(response.status, '请稍后重试'))
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        payload.extend(chunk)
                        if len(payload) > 256 * 1024:
                            raise ValueError('模型回复超出接收限制')
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ValueError('模型服务连接超时或不可达') from None
        try:
            value = json.loads(payload)
            choice = value['choices'][0]
            result = choice['message']['content']
            if choice['finish_reason'] != 'stop' or not isinstance(result, str) or not result.strip():
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise ValueError('模型没有返回完整的文本回复，请核对模型类型或额度') from None
        usage = value.get('usage')
        counts = {}
        if isinstance(usage, dict):
            for source, target in (('prompt_tokens', 'inputTokens'), ('completion_tokens', 'outputTokens'), ('total_tokens', 'totalTokens')):
                count = usage.get(source)
                if type(count) is int and 0 <= count < 2**63:
                    counts[target] = count
        return result.strip(), counts or None

    async def run(self, task):
        ident = task['id']
        try:
            record = self.connection(task['user_id'], task['connection_id'])
            if not record:
                raise ValueError('模型连接已移除，请重新配置')
            key = self.token(record)
            await self.hub.change(ident, state='running', summary='模型正在生成回复')
            result, usage = await self.complete(record, key, task['prompt'])
        except ValueError as exc:
            await self.hub.change(ident, state='failed', summary=str(exc))
            return
        self.store.set_usage(ident, usage)
        await self.hub.change(ident, state='completed', summary=result[:900], result=result)
