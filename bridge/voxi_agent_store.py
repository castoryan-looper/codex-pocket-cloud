"""Private, durable Voxi task records and write-only provider credentials.

This is a personal/small-pilot control plane. Workspace separation is not a
multi-tenant execution sandbox; public paid hosting needs isolated workers.
"""
import base64
import copy
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from voxi_voices import validate_voice
from voxi_channel_tools import validate_channel_personalization

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'delivery_unknown'}
LIVE = {'queued', 'submitting', 'running', 'needs_input', 'awaiting_approval', 'cancelling'}
EXTERNAL_AGENT_LABELS = {'openclaw': 'OpenClaw', 'muse': 'Muse Code'}
CHANNEL_LIMIT = 16
MISSING_CHANNEL = {'name': '', 'mode': 'disabled'}


def channel_routes(channels):
    return {route.get('slot', index): route for index, route in enumerate(channels)}


def route_engine(route):
    return {'agent': 'codex', 'openclaw': 'openclaw', 'muse': 'muse', 'model': 'model'}.get(route['mode'])
DEFAULTS = {
    'enabled': False, 'model': '', 'maxConcurrent': 2, 'maxTaskSeconds': 1800,
    'instructions': '你是 Voxi Agent。持续完成用户明确委托的工作。只播报必要状态，最终先给简短结论，再给详细结果。',
    'channels': [
        {'name': '快问', 'mode': 'fast'}, {'name': 'Voxi Agent', 'mode': 'agent'},
    ],
}


class AgentStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'voxi-agent.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,device_id TEXT,channel INTEGER NOT NULL,
          request_id TEXT NOT NULL,prompt TEXT NOT NULL,state TEXT NOT NULL,summary TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '',thread_id TEXT,turn_id TEXT,
          created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(user_id,request_id));
        CREATE INDEX IF NOT EXISTS task_owner ON tasks(user_id,channel,created);
        CREATE INDEX IF NOT EXISTS task_turn ON tasks(thread_id,turn_id);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,state TEXT NOT NULL,
          summary TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals(task_id TEXT PRIMARY KEY,request TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS secrets(provider TEXT PRIMARY KEY,ciphertext TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS model_connections(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,provider TEXT NOT NULL,
          model TEXT NOT NULL,workspace TEXT NOT NULL,secret BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS usage(task_id TEXT PRIMARY KEY,input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER);
        CREATE TABLE IF NOT EXISTS thread_usage(thread_id TEXT PRIMARY KEY,total_tokens INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS voice_usage(
          user_id TEXT NOT NULL,response_id TEXT NOT NULL,kind TEXT NOT NULL,
          input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER NOT NULL,
          PRIMARY KEY(user_id,response_id));
        ''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(tasks)')}
        for name,definition in [('engine',"TEXT NOT NULL DEFAULT 'codex'"),('connection_id','TEXT'),
                                ('runtime_tools',"TEXT NOT NULL DEFAULT ''")]:
            if name not in columns:self.db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
        for task in self.db.execute("SELECT id FROM tasks WHERE state NOT IN ('queued','completed','failed','cancelled','interrupted','delivery_unknown')").fetchall():
            self.update(task['id'], state='interrupted', summary='服务已重启；未自动重放任务，请核对结果后继续')
        with self.db:
            self.db.execute('DELETE FROM approvals')

    def close(self):
        self.db.close()

    def settings(self, user=None):
        row = self.db.execute('SELECT value FROM config WHERE key=?', ('settings',)).fetchone()
        value = json.loads(row['value']) if row else copy.deepcopy(DEFAULTS)
        # Legacy deployments stored editable shared channels in this record.
        value['channels'] = copy.deepcopy(DEFAULTS['channels'])
        if user:
            override = self.db.execute('SELECT value FROM config WHERE key=?', ('channels:' + user,)).fetchone()
            if override:
                value['channels'] = json.loads(override['value'])
        return value

    @staticmethod
    def validate_channels(channels):
        if not isinstance(channels, list) or len(channels) > CHANNEL_LIMIT:
            raise ValueError(f'最多可配置 {CHANNEL_LIMIT} 个频道')
        result, slots = [], set()
        for index, c in enumerate(channels):
            if not isinstance(c, dict) or not {'name','mode'} <= c.keys() or c.keys() - {'name','mode','connectionId','slot','voice','role','skills','mcp'}:
                raise ValueError('频道需要名称和模式')
            slot = c.get('slot', index)
            if type(slot) is not int or not 0 <= slot < CHANNEL_LIMIT or slot in slots:
                raise ValueError('频道编号无效或重复')
            slots.add(slot)
            if not isinstance(c['name'], str) or not 1 <= len(c['name'].strip()) <= 32 or c['mode'] not in ('fast', 'agent', 'openclaw', 'muse', 'model', 'disabled'):
                raise ValueError('频道名称或模式无效')
            route = {'name': c['name'].strip(), 'mode': c['mode']}
            route.update(validate_channel_personalization(c))
            if 'slot' in c: route['slot'] = slot
            if 'voice' in c:
                voice = validate_voice(c['voice'], inherit=True)
                if voice: route['voice'] = voice
            if c['mode'] in (*EXTERNAL_AGENT_LABELS, 'model'):
                if not isinstance(c.get('connectionId'),str) or not 1<=len(c['connectionId'])<=64:
                    raise ValueError('请为频道选择已配置的接入')
                route['connectionId'] = c['connectionId']
            result.append(route)
        return result

    def reset_channels(self, user):
        with self.db:self.db.execute('DELETE FROM config WHERE key=?',('channels:'+user,))

    def channel_overrides(self):
        return [r['key'][9:] for r in self.db.execute("SELECT key FROM config WHERE key LIKE 'channels:%'")]

    def preferences(self, user):
        row=self.db.execute('SELECT value FROM config WHERE key=?',('preferences:'+user,)).fetchone()
        return {'fastPrompt':'','voice':'',**(json.loads(row['value']) if row else {})}

    def save_preferences(self, user, value):
        if not isinstance(value,dict) or set(value) not in ({'fastPrompt'},{'fastPrompt','voice'}) or not isinstance(value['fastPrompt'],str) or len(value['fastPrompt'])>4000:
            raise ValueError('回答偏好最多4000字符')
        value={**self.preferences(user),**value};validate_voice(value['voice'],inherit=True)
        with self.db:self.db.execute('INSERT OR REPLACE INTO config VALUES(?,?)',('preferences:'+user,json.dumps(value,ensure_ascii=False)))

    def save_settings(self, value, user=None):
        if user:
            value = self.validate_channels(value)
            key = 'channels:' + user
        else:
            if not isinstance(value, dict) or set(value) != set(DEFAULTS):
                raise ValueError('配置字段不完整')
            if type(value['enabled']) is not bool or type(value['maxConcurrent']) is not int or not 1 <= value['maxConcurrent'] <= 4:
                raise ValueError('并发数量需要 1–4')
            if type(value['maxTaskSeconds']) is not int or not 60 <= value['maxTaskSeconds'] <= 7200:
                raise ValueError('单任务最长时间需要 60–7200 秒')
            if not isinstance(value['model'], str) or len(value['model']) > 100 or not isinstance(value['instructions'], str) or len(value['instructions']) > 8000:
                raise ValueError('模型名称或指令过长')
            if value['channels'] != DEFAULTS['channels']:
                raise ValueError('初始频道固定为快问和 Voxi Agent，请在个人空间管理自定义频道')
            key = 'settings'
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO config VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))
        return self.settings(user)

    def task(self, ident, user=None):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?' + (' AND user_id=?' if user else ''),
                              (ident, user) if user else (ident,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, user=None, channel=None, limit=100, *, route=None):
        clauses, args = [], []
        if user:
            clauses.append('user_id=?'); args.append(user)
        if channel is not None:
            clauses.append('channel=?'); args.append(channel)
        if route is not None:
            clauses.extend(('engine=?', 'connection_id IS ?'))
            args.extend((route_engine(route), route.get('connectionId')))
        query = 'SELECT * FROM tasks' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        return [dict(r) for r in self.db.execute(query + ' ORDER BY created DESC LIMIT ?', (*args, min(200, limit)))]

    def has_active_tasks(self, user):
        states = ','.join('?' for _ in LIVE)
        return self.db.execute(f'SELECT 1 FROM tasks WHERE user_id=? AND state IN ({states}) LIMIT 1', (user, *LIVE)).fetchone() is not None

    def submit(self, user, device, channel, request_id, prompt, thread=None, *, engine='codex', connection=None, runtime_tools=''):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('需要 1–128 字符的请求编号')
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000 or type(channel) is not int or not 0 <= channel < CHANNEL_LIMIT:
            raise ValueError('任务内容或频道无效')
        old = self.db.execute('SELECT * FROM tasks WHERE user_id=? AND request_id=?', (user, request_id)).fetchone()
        if old:
            if old['prompt'] != prompt.strip() or old['channel'] != channel:
                raise ValueError('同一请求编号不能对应不同任务')
            return dict(old), False
        if self.db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','submitting','running','needs_input','awaiting_approval','cancelling')").fetchone()[0] >= 32:
            raise ValueError('任务队列已满，请等待已有任务完成')
        ident, now = uuid.uuid4().hex, time.time()
        with self.db:
            if engine in EXTERNAL_AGENT_LABELS and not thread:thread='voxi-'+user+'-'+ident
            self.db.execute('INSERT INTO tasks(id,user_id,device_id,channel,request_id,prompt,state,summary,result,thread_id,turn_id,created,updated,engine,connection_id,runtime_tools) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (ident,user,device,channel,request_id,prompt.strip(),'queued','任务已保存，等待执行','',thread,None,now,now,engine,connection,runtime_tools))
            self.db.execute('INSERT INTO events(task_id,state,summary,created) VALUES(?,?,?,?)', (ident,'queued','任务已保存，等待执行',now))
        return self.task(ident), True

    def update(self, ident, **values):
        allowed = {'state','summary','result','thread_id','turn_id','runtime_tools'}
        if not values or not set(values) <= allowed:
            raise ValueError('无效任务更新')
        if 'state' in values and values['state'] not in LIVE | TERMINAL:
            raise ValueError('无效状态')
        values['updated'] = time.time()
        with self.db:
            self.db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), ident))
            task = self.task(ident)
            if task and ('state' in values or 'summary' in values):
                self.db.execute('INSERT INTO events(task_id,state,summary,created) VALUES(?,?,?,?)',
                                (ident,task['state'],task['summary'],values['updated']))
                self.db.execute('DELETE FROM events WHERE task_id=? AND seq NOT IN (SELECT seq FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 80)', (ident,ident))
            if task and task['state'] in TERMINAL:
                self.db.execute('DELETE FROM approvals WHERE task_id=?', (ident,))
        return task

    def approval(self, ident, request=None):
        row = self.db.execute('SELECT request FROM approvals WHERE task_id=?', (ident,)).fetchone()
        pending = json.loads(row['request']) if row else []
        if isinstance(pending,dict):pending=[pending]
        if request is not None:
            if not any(p.get('id')==request.get('id') for p in pending):pending.append(request)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO approvals VALUES(?,?)', (ident,json.dumps(pending,ensure_ascii=False)))
        return pending[0] if pending else None

    def pop_approval(self, ident, request_id):
        row=self.db.execute('SELECT request FROM approvals WHERE task_id=?',(ident,)).fetchone()
        pending=json.loads(row['request']) if row else []
        if isinstance(pending,dict):pending=[pending]
        pending=[p for p in pending if p.get('id')!=request_id]
        with self.db:
            if pending:self.db.execute('UPDATE approvals SET request=? WHERE task_id=?',(json.dumps(pending,ensure_ascii=False),ident))
            else:self.db.execute('DELETE FROM approvals WHERE task_id=?',(ident,))

    def public_task(self, task, detail=False):
        fields = ('id','user_id','device_id','channel','state','summary','created','updated','engine','connection_id')
        value = {k:task[k] for k in fields}
        if detail:
            value.update(prompt=task['prompt'],result=task['result'],threadId=task['thread_id'],turnId=task['turn_id'])
            value['events'] = [dict(r) for r in self.db.execute('SELECT seq,state,summary,created FROM events WHERE task_id=? ORDER BY seq', (task['id'],))]
            request = self.approval(task['id'])
            if request:
                params = request.get('params', {})
                value['approval'] = {'method':request['method'],'command':params.get('command'),
                                     'reason':params.get('reason'),'questions':params.get('questions',[])}
            usage = self.db.execute('SELECT input_tokens,output_tokens,total_tokens FROM usage WHERE task_id=?', (task['id'],)).fetchone()
            value['usage'] = dict(usage) if usage else None
        return value

    def set_usage(self, ident, usage):
        if not isinstance(usage, dict): return
        def count(key):
            n=usage.get(key); return n if type(n) is int and 0 <= n <= 10**12 else None
        with self.db:
            self.db.execute('''INSERT INTO usage VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
              input_tokens=coalesce(excluded.input_tokens,usage.input_tokens),
              output_tokens=coalesce(excluded.output_tokens,usage.output_tokens),
              total_tokens=coalesce(excluded.total_tokens,usage.total_tokens)''',
                            (ident,count('inputTokens'),count('outputTokens'),count('totalTokens')))

    def product_metrics(self):
        row = self.db.execute("""SELECT count(*) AS tasks,
          count(CASE WHEN state='queued' THEN 1 END) AS queued,
          count(CASE WHEN state='completed' THEN 1 END) AS completed,
          count(CASE WHEN state IN ('failed','interrupted','delivery_unknown') THEN 1 END) AS unresolved,
          count(DISTINCT CASE WHEN engine='codex' THEN thread_id END) AS codexThreads
          FROM tasks""").fetchone()
        covered, tokens = self.db.execute('SELECT count(*),sum(total_tokens) FROM thread_usage').fetchone()
        return {**dict(row),'threadsWithUsage':covered,'totalTokens':tokens}

    def user_usage(self, user):
        rows = self.db.execute('''SELECT t.engine,
          count(*) AS tasks, count(u.total_tokens) AS measured,
          sum(u.input_tokens) AS inputTokens, sum(u.output_tokens) AS outputTokens,
          sum(u.total_tokens) AS totalTokens
          FROM tasks t LEFT JOIN usage u ON t.id=u.task_id
          WHERE t.user_id=? GROUP BY t.engine ORDER BY t.engine''', (user,)).fetchall()
        voice = self.db.execute('''SELECT kind,count(*) AS responses,sum(total_tokens) AS totalTokens,
          sum(input_tokens) AS inputTokens,sum(output_tokens) AS outputTokens
          FROM voice_usage WHERE user_id=? GROUP BY kind ORDER BY kind''',(user,)).fetchall()
        totals = [r['totalTokens'] for r in [*rows,*voice] if r['totalTokens'] is not None]
        return {'totalTokens':sum(totals) if totals else None,
                'tasks':sum(r['tasks'] for r in rows),'measuredTasks':sum(r['measured'] for r in rows),
                'engines':[dict(r) for r in rows],'voice':[dict(r) for r in voice]}

    def record_voice_usage(self, user, response_id, kind, usage):
        if not isinstance(response_id,str) or not 1<=len(response_id)<=200 or not isinstance(usage,dict):
            return
        def count(key):
            value=usage.get(key)
            return value if type(value) is int and 0<=value<=10**12 else None
        total=count('total_tokens')
        if total is None:
            return
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO voice_usage VALUES(?,?,?,?,?,?)',
                            (user,response_id,kind,count('input_tokens'),count('output_tokens'),total))

    def record_thread_usage(self, thread, total):
        if not isinstance(total, dict):
            return
        tokens = total.get('totalTokens')
        if type(tokens) is not int or not 0 <= tokens <= 2**63-1:
            return
        with self.db:
            self.db.execute("""INSERT INTO thread_usage(thread_id,total_tokens)
                SELECT ?,? WHERE EXISTS(SELECT 1 FROM tasks WHERE thread_id=? AND engine='codex')
                ON CONFLICT(thread_id) DO UPDATE SET total_tokens=excluded.total_tokens
                WHERE excluded.total_tokens>thread_usage.total_tokens""", (thread, tokens, thread))

    def _cipher(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        path = self.directory / 'voxi-credentials.key'
        if not path.exists():
            connections=self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='openclaw_connections'").fetchone()
            encrypted_connections=connections and self.db.execute("SELECT count(*) FROM openclaw_connections WHERE transport='https'").fetchone()[0]
            if self.db.execute('SELECT count(*) FROM secrets').fetchone()[0] or encrypted_connections or self.db.execute('SELECT 1 FROM model_connections LIMIT 1').fetchone():
                raise ValueError('凭据加密密钥缺失，请恢复备份，不能重新生成')
            try:
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                    stream.write(secrets.token_bytes(32))
            except FileExistsError:
                pass
        return AESGCM(path.read_bytes())

    def save_secret(self, provider, key):
        if provider not in ('openai','anthropic') or not isinstance(key,str) or len(key)>512:
            raise ValueError('供应商或密钥无效')
        if key and (len(key.strip()) < 12 or any(c.isspace() for c in key.strip())):
            raise ValueError('密钥格式无效')
        with self.db:
            if not key:
                self.db.execute('DELETE FROM secrets WHERE provider=?',(provider,))
            else:
                nonce=secrets.token_bytes(12)
                encrypted=self._cipher().encrypt(nonce,key.strip().encode(),provider.encode())
                self.db.execute('INSERT OR REPLACE INTO secrets VALUES(?,?,?)',(provider,base64.b64encode(nonce+encrypted).decode(),time.time()))

    def credentials(self):
        saved={r['provider']:r['updated'] for r in self.db.execute('SELECT provider,updated FROM secrets')}
        return {p:{'configured':p in saved,'updated':saved.get(p),'active':False} for p in ('openai','anthropic')}
