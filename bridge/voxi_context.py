"""Owner-scoped, read-only external conversation snapshots."""
import hashlib
import json
import re
import secrets
import time
import uuid

from conversation_memory import terms


SOURCES = {'codex', 'claude-code', 'claude', 'open-webui', 'ollama'}
LIVE_SOURCES = {'codex', 'claude-code', 'open-webui'}
SECRET = re.compile(r'(?i)\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,}|Bearer\s+[a-z0-9._~+/=-]{16,})')
COMMON_TERMS = set('the this that and with for you your please task continue resume help last from are can have 继续 上次 任务 帮我 请你 现在 怎么 如何 这个 一下 进展 最新 上面'.split())


def clean(value, limit):
    if not isinstance(value, str):
        return ''
    safe = value[:limit*2].encode('utf-8','replace').decode('utf-8').replace('\x00','')
    return SECRET.sub('[已隐藏密钥]',safe).strip()[:limit]


class ContextLibrary:
    def __init__(self, db):
        self.db = db
        db.executescript('''
          CREATE TABLE IF NOT EXISTS context_connectors(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source TEXT NOT NULL,
            name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE, seen INTEGER);
          CREATE TABLE IF NOT EXISTS context_chats(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source TEXT NOT NULL,
            external_id TEXT NOT NULL, title TEXT NOT NULL, updated INTEGER NOT NULL,
            messages TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,source,external_id));
          CREATE INDEX IF NOT EXISTS context_owner ON context_chats(user_id,removed,updated);
        ''')

    def snapshot(self, uid):
        chats = self.db.execute('SELECT id,source,title,updated,pinned FROM context_chats WHERE user_id=? AND removed=0 ORDER BY updated DESC,id', (uid,))
        connectors = self.db.execute('SELECT id,source,name,seen FROM context_connectors WHERE user_id=? ORDER BY name,id', (uid,))
        return {'chats': [dict(r) for r in chats], 'connectors': [dict(r) for r in connectors]}

    def save(self, uid, source, chat, *, manual=False):
        if not isinstance(source,str) or source not in SOURCES or not isinstance(chat,dict):
            raise ValueError('来源或会话格式无效')
        external = chat.get('id')
        if not isinstance(external,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',external):
            raise ValueError('会话需要稳定编号')
        title = clean(chat.get('title'),120) or '未命名会话'
        messages = chat.get('messages')
        if not isinstance(messages,list) or not 1 <= len(messages) <= 16:
            raise ValueError('每次导入最多 16 条近期消息')
        safe = []
        for message in messages:
            if not isinstance(message,dict) or message.get('role') not in ('user','assistant'):
                raise ValueError('只支持用户和助手的文本消息')
            text = clean(message.get('text'),1600)
            if text: safe.append({'role':message['role'],'text':text})
        if not safe:
            raise ValueError('会话没有可导入的文本')
        updated = chat.get('updated')
        if type(updated) is not int or not 0 < updated <= time.time()*1000+300000:
            raise ValueError('会话更新时间无效')
        encoded = json.dumps(safe,ensure_ascii=False,separators=(',',':'))
        old = self.db.execute('SELECT * FROM context_chats WHERE user_id=? AND source=? AND external_id=?',
                              (uid,source,external)).fetchone()
        if old and (old['removed'] and not manual or old['updated']>updated and not old['removed']):
            return False
        if old and not old['removed'] and old['title']==title and old['messages']==encoded and old['updated']==updated:
            return False
        if (not old or old['removed']) and self.db.execute(
                'SELECT count(*) FROM context_chats WHERE user_id=? AND removed=0',(uid,)).fetchone()[0] >= 120:
            raise ValueError('每个账户最多 120 个会话，请先移除旧会话')
        with self.db:
            self.db.execute('''INSERT INTO context_chats(id,user_id,source,external_id,title,updated,messages)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,source,external_id) DO UPDATE SET
              title=excluded.title,updated=excluded.updated,messages=excluded.messages,removed=0''',
                            (uuid.uuid4().hex,uid,source,external,title,updated,encoded))
        return True

    def authorize(self, uid, source, name):
        if not isinstance(source,str) or source not in LIVE_SOURCES or not clean(name,64):
            raise ValueError('请选择自动同步来源并填写连接名称')
        if self.db.execute('SELECT count(*) FROM context_connectors WHERE user_id=?',(uid,)).fetchone()[0]>=12:
            raise ValueError('每个账户最多 12 个同步连接')
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('INSERT INTO context_connectors VALUES(?,?,?,?,?,NULL)',
                            (uuid.uuid4().hex,uid,source,clean(name,64),hashlib.sha256(token.encode()).hexdigest()))
        return {'token':token}

    def ingest(self, token, enabled_users, chat):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token) or not isinstance(enabled_users,list):
            raise ValueError('同步授权无效')
        connection = self.db.execute('SELECT * FROM context_connectors WHERE digest=?',
                                     (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not connection or connection['user_id'] not in enabled_users:
            raise ValueError('同步授权无效或已撤销')
        changed = self.save(connection['user_id'],connection['source'],chat) if chat is not None else False
        with self.db:
            self.db.execute('UPDATE context_connectors SET seen=? WHERE id=?',
                            (int(time.time()*1000),connection['id']))
        return {'ok':True,'changed':changed}

    def manage(self, uid, body):
        action, ident = body['action'], body.get('id')
        if action=='context_import':
            return {'changed':self.save(uid,body.get('source'),body.get('chat'),manual=True)}
        if action=='context_authorize':
            return self.authorize(uid,body.get('source'),body.get('name'))
        if action=='context_list':
            return self.snapshot(uid)
        if not isinstance(ident,str):
            raise ValueError('需要有效的会话或连接编号')
        if action=='context_revoke':
            with self.db:
                self.db.execute('DELETE FROM context_connectors WHERE id=? AND user_id=?',(ident,uid))
            return self.snapshot(uid)
        chat = self.db.execute('SELECT * FROM context_chats WHERE id=? AND user_id=? AND removed=0',(ident,uid)).fetchone()
        if not chat:
            raise ValueError('会话不存在')
        if action=='context_detail':
            return {'title':chat['title'],'messages':json.loads(chat['messages'])}
        with self.db:
            if action=='context_remove':
                self.db.execute("UPDATE context_chats SET removed=1,pinned=0,title='',messages='[]' WHERE id=?",(ident,))
            elif action=='context_pin':
                self.db.execute('UPDATE context_chats SET pinned=0 WHERE user_id=?',(uid,))
                if not chat['pinned']:
                    self.db.execute('UPDATE context_chats SET pinned=1 WHERE id=?',(ident,))
            else:
                raise ValueError('无效的上下文操作')
        return self.snapshot(uid)

    def reference(self, uid, query):
        keys = terms(query) - COMMON_TERMS
        follow = bool(re.search(r'继续|接着|上次|进展|\b(continue|resume|pick up)\b',query,re.I))
        candidates = []
        for row in self.db.execute('SELECT * FROM context_chats WHERE user_id=? AND removed=0 ORDER BY updated DESC',(uid,)):
            title_keys = keys & terms(row['title'])
            title_score = len(title_keys)
            messages = json.loads(row['messages'])
            # One incidental shared word must not disclose an unrelated chat.
            match = max((len(keys & terms(m['text'])) for m in messages),default=0)
            score = title_score*3 + match
            if match >= 2 or title_score >= 2 or any(key.isascii() and len(key)>=4 for key in title_keys) or (follow and row['pinned']):
                candidates.append((bool(follow and row['pinned']),score,row,messages))
        if not candidates:
            return ''
        _,_,row,messages = max(candidates,key=lambda item:item[:2])
        recent = [{'角色':m['role'],'文本':m['text'][:600]} for m in messages[-4:]]
        return json.dumps({'来源':row['source'],'会话':row['title'],'近期消息':recent},ensure_ascii=False)[:3600]
