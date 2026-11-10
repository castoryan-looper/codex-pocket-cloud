"""User-authorized model and published app connections."""
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
    'doubao': {'name': '豆包 · 火山方舟', 'modelLabel': '模型 ID 或推理接入点 ID',
               'docs': 'https://docs.volcengine.com/docs/ark/text-generation?lang=zh'},
    'kimi': {'name': 'Kimi · 月之暗面（中国）', 'docs': 'https://platform.moonshot.cn/docs/'},
    'minimax': {'name': 'MiniMax（国际站）',
                'docs': 'https://platform.minimax.io/docs/api-reference/text-openai-api'},
    'glm': {'name': 'GLM · 智谱开放平台（中国）', 'docs': 'https://docs.bigmodel.cn/'},
    'openai': {'name': 'OpenAI · GPT', 'docs': 'https://developers.openai.com/api/docs/quickstart'},
    'anthropic': {'name': 'Anthropic · Claude', 'docs': 'https://platform.claude.com/docs/en/api/messages/create'},
    'gemini': {'name': 'Google · Gemini', 'docs': 'https://ai.google.dev/gemini-api/docs/openai'},
    'openrouter': {'name': 'OpenRouter', 'category': 'aggregator', 'modelLabel': '模型 ID（供应商/模型）',
                   'docs': 'https://openrouter.ai/docs/quickstart'},
    'dify-chat': {'name': 'Dify Cloud · 对话应用', 'category': 'app', 'defaultModel': 'chat', 'fixedModel': True,
                  'continuation': True, 'docs': 'https://docs.dify.ai/en/api-reference/guides/chatflow'},
    'dify-workflow': {'name': 'Dify Cloud · 工作流', 'category': 'app', 'defaultModel': 'query',
                      'modelLabel': '输入变量名', 'workspaceRequired': True, 'workspaceLabel': '输出变量名',
                      'defaultWorkspace': 'result', 'docs': 'https://docs.dify.ai/en/api-reference/guides/workflow'},
}

CHAT_ENDPOINTS = {
    'deepseek': 'https://api.deepseek.com/chat/completions',
    'doubao': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions',
    'kimi': 'https://api.moonshot.cn/v1/chat/completions',
    'minimax': 'https://api.minimax.io/v1/chat/completions',
    'glm': 'https://open.bigmodel.cn/api/paas/v4/chat/completions',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
    'openrouter': 'https://openrouter.ai/api/v1/chat/completions',
}
DIFY_MODES = {'dify-chat': {'chat', 'advanced-chat', 'agent-chat'}, 'dify-workflow': {'workflow'}}
DIFY_API = 'https://api.dify.ai/v1'
MAX_RESPONSE = 256 * 1024
CHAT_USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens')
NATIVE_USAGE = ('input_tokens', 'output_tokens', 'total_tokens')


def token_counts(usage, fields=CHAT_USAGE):
    if not isinstance(usage, dict):
        return None
    counts = {}
    for source, target in zip(fields, ('inputTokens', 'outputTokens', 'totalTokens')):
        count = usage.get(source) if source else None
        if type(count) is int and 0 <= count <= 10**12:
            counts[target] = count
    return counts or None


def bearer(key):
    return {'Authorization': 'Bearer ' + key}


def response_status(response):
    if response.status == 200:
        return
    messages = {400: '请检查配置和接口兼容性', 401: '请检查 API Key', 403: '请检查接口权限',
                404: '请检查模型、应用或业务空间', 429: '请检查调用额度或速率限制'}
    raise ValueError(f'服务返回 HTTP {response.status}。' + messages.get(response.status, '请稍后重试'))


async def request_json(client, method, url, headers, **options):
    async with client.request(method, url, headers=headers, allow_redirects=False, **options) as response:
        response_status(response)
        payload = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            payload.extend(chunk)
            if len(payload) > MAX_RESPONSE:
                raise ValueError('服务回复超出接收限制')
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise ValueError('服务未返回有效 JSON') from None
    if not isinstance(value, dict):
        raise ValueError('服务返回格式无效')
    return value


def model_request(record, key, text, max_tokens):
    provider, model = record['provider'], record['model']
    messages = [{'role': 'user', 'content': text}]
    if provider == 'openai':
        return 'https://api.openai.com/v1/responses', bearer(key), {
            'model': model, 'input': text, 'max_output_tokens': max_tokens, 'store': False,
        }
    if provider == 'anthropic':
        return 'https://api.anthropic.com/v1/messages', {
            'x-api-key': key, 'anthropic-version': '2023-06-01',
        }, {'model': model, 'messages': messages, 'max_tokens': max_tokens}
    body = {'model': model, 'messages': messages, 'stream': False}
    body['max_completion_tokens' if provider == 'minimax' else 'max_tokens'] = max_tokens
    if provider == 'qwen':
        body['enable_thinking'] = False
        url = f"https://{record['workspace']}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    else:
        url = CHAT_ENDPOINTS[provider]
        if provider == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
        elif provider == 'minimax':
            body['reasoning_split'] = True
    return url, bearer(key), body


def model_result(provider, value):
    try:
        if provider == 'openai':
            if value['status'] != 'completed':
                raise ValueError
            result = '\n'.join(block['text'] for item in value['output'] if item['type'] == 'message'
                               for block in item['content'] if block['type'] == 'output_text')
        elif provider == 'anthropic':
            if value['stop_reason'] not in ('end_turn', 'stop_sequence'):
                raise ValueError
            result = '\n'.join(block['text'] for block in value['content'] if block['type'] == 'text')
        else:
            choice = value['choices'][0]
            if choice['finish_reason'] != 'stop':
                raise ValueError
            result = choice['message']['content']
        if not isinstance(result, str) or not result.strip():
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError('模型没有返回完整的文本回复，请核对模型类型或额度') from None
    usage = value.get('usage')
    counts = token_counts(usage, NATIVE_USAGE if provider in ('openai', 'anthropic') else CHAT_USAGE)
    if provider == 'anthropic' and counts and {'inputTokens', 'outputTokens'} <= counts.keys():
        cache = (usage.get('cache_creation_input_tokens', 0), usage.get('cache_read_input_tokens', 0))
        if all(type(count) is int and 0 <= count <= 10**12 for count in cache):
            counts['inputTokens'] += sum(cache)
            counts['totalTokens'] = counts['inputTokens'] + counts['outputTokens']
    return result.strip(), counts


def dify_inputs(parameters, workflow_input):
    fields = parameters.get('user_input_form')
    if not isinstance(fields, list):
        raise ValueError('Dify 未返回应用输入配置')
    supported = not workflow_input
    defaults = {}
    for field in fields:
        if not isinstance(field, dict) or len(field) != 1:
            raise ValueError('Dify 应用输入配置无法识别')
        kind, spec = next(iter(field.items()))
        if not isinstance(spec, dict) or not isinstance(spec.get('variable'), str):
            raise ValueError('Dify 应用输入配置无法识别')
        variable = spec['variable']
        if variable == workflow_input:
            supported = kind in ('text-input', 'paragraph')
        elif spec.get('required'):
            default = spec.get('default')
            if type(default) not in (str, int, float, bool) or default == '':
                raise ValueError('Dify 应用有 Voxi 无法填写的必填输入，请配置默认值')
            defaults[variable] = default
    if not supported:
        raise ValueError('Dify 工作流的输入变量必须是已发布的文本字段')
    return defaults


async def dify_result(response, output_variable):
    response_status(response)
    answer = []
    size = 0
    async for line in response.content:
        size += len(line)
        if size > MAX_RESPONSE:
            raise ValueError('Dify 回复超出接收限制')
        if not line.startswith(b'data:'):
            continue
        try:
            event = json.loads(line[5:])
            kind = event['event']
            if kind == 'error':
                raise ValueError('Dify 应用执行失败，请查看应用运行日志')
            if kind == 'human_input_required':
                raise ValueError('Dify 应用正在等待人工输入，请到 Dify 处理')
            if output_variable and kind == 'workflow_finished':
                data = event['data']
                if data['status'] != 'succeeded':
                    raise ValueError('Dify 工作流未成功结束，请查看应用运行日志')
                result = data['outputs'].get(output_variable)
                if not isinstance(result, str) or not result.strip():
                    raise ValueError('Dify 工作流未返回配置的文本输出变量')
                return result.strip(), token_counts(data, (None, None, 'total_tokens')), None
            if not output_variable and kind in ('message', 'agent_message', 'message_replace'):
                if not isinstance(event['answer'], str):
                    raise TypeError
                if kind == 'message_replace':
                    answer.clear()
                answer.append(event['answer'])
            if not output_variable and kind == 'message_end':
                result = ''.join(answer).strip()
                if not result:
                    raise ValueError('Dify 对话应用未返回文本')
                thread = event.get('conversation_id')
                if not isinstance(thread, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,128}', thread):
                    thread = None
                return result, token_counts((event.get('metadata') or {}).get('usage')), thread
        except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError):
            raise ValueError('Dify 返回格式无效') from None
    raise ValueError('Dify 应用连接提前关闭，远端任务可能仍在执行')


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
        if provider == 'dify-chat':
            model = 'chat'
        if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,128}', model):
            raise ValueError('请填写有效的模型 ID')
        if provider == 'qwen':
            if not isinstance(workspace, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', workspace) or workspace.endswith('-'):
                raise ValueError('请填写百炼北京地域的业务空间 ID')
        elif provider == 'dify-workflow':
            if not isinstance(workspace, str) or not all(re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,62}', value)
                                                        for value in (model, workspace)):
                raise ValueError('请填写有效的 Dify 输入和输出变量名')
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
            raise ValueError('每个账户最多 16 个模型或应用接入')
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
            provider = record['provider']
            if provider in DIFY_MODES:
                async with self.client(9) as client:
                    async with asyncio.timeout(9):
                        info = await request_json(client, 'GET', DIFY_API + '/info', bearer(key))
                        if info.get('mode') not in DIFY_MODES[provider]:
                            raise ValueError('Dify 应用类型不匹配，请检查应用密钥和选择的接入类型')
                        await self.dify_parameters(client, record, key)
            else:
                await self.complete(record, key, 'Reply with OK.', seconds=9,
                                    max_tokens=64 if provider in ('deepseek', 'qwen') else 1024)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ValueError('服务连接超时或不可达') from None
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

    @staticmethod
    def client(seconds):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds, connect=min(5, seconds)),
                                     trust_env=False, read_bufsize=MAX_RESPONSE)

    async def complete(self, record, key, text, *, seconds=90, max_tokens=1800):
        url, headers, body = model_request(record, key, text, max_tokens)
        try:
            async with self.client(seconds) as client:
                value = await request_json(client, 'POST', url, headers, json=body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ValueError('模型服务连接超时或不可达') from None
        return model_result(record['provider'], value)

    async def dify_parameters(self, client, record, key):
        parameters = await request_json(client, 'GET', DIFY_API + '/parameters', bearer(key))
        return dify_inputs(parameters, record['model'] if record['provider'] == 'dify-workflow' else None)

    async def complete_dify(self, record, key, task):
        workflow = record['provider'] == 'dify-workflow'
        path = '/workflows/run' if workflow else '/chat-messages'
        try:
            async with self.client(90) as client:
                inputs = await self.dify_parameters(client, record, key)
                body = {'user': 'voxi-' + record['id'], 'inputs': inputs, 'response_mode': 'streaming'}
                if workflow:
                    inputs[record['model']] = task['prompt']
                else:
                    body.update(query=task['prompt'], auto_generate_name=False)
                    if task.get('thread_id'):
                        body['conversation_id'] = task['thread_id']
                async with client.post(DIFY_API + path, headers=bearer(key), json=body, allow_redirects=False) as response:
                    return await dify_result(response, record['workspace'] if workflow else None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ValueError('Dify 连接超时或中断，远端任务可能仍在执行') from None

    async def run(self, task):
        ident = task['id']
        try:
            record = self.connection(task['user_id'], task['connection_id'])
            if not record:
                raise ValueError('模型连接已移除，请重新配置')
            key = self.token(record)
            is_app = record['provider'] in DIFY_MODES
            await self.hub.change(ident, state='running', summary='Dify 应用正在执行' if is_app else '模型正在生成回复')
            thread = None
            if is_app:
                result, usage, thread = await self.complete_dify(record, key, task)
            else:
                result, usage = await self.complete(record, key, task['prompt'])
        except ValueError as exc:
            await self.hub.change(ident, state='failed', summary=str(exc))
            return
        self.store.set_usage(ident, usage)
        await self.hub.change(ident, state='completed', summary=result[:900], result=result, thread_id=thread)
