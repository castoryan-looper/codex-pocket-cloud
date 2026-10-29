"""Explicitly imported private conversation data; never treat it as instructions."""
import json
import re
from datetime import datetime
from pathlib import Path

MAX_BYTES = 1024 * 1024


def validate(payload):
    if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('messages'), list):
        raise ValueError('invalid conversation format')
    messages = payload['messages']
    if len(messages) > 2000:
        raise ValueError('too many messages')
    for m in messages:
        if not isinstance(m, dict) or m.get('role') not in ('user', 'assistant') or not isinstance(m.get('text'), str):
            raise ValueError('invalid message')
        if len(m['text']) > 100000:
            raise ValueError('message too long')
    return payload


def load(path):
    try:
        p = Path(path)
        if p.stat().st_size > MAX_BYTES: return []
        return validate(json.loads(p.read_text('utf-8')))['messages']
    except (OSError, ValueError, TypeError, UnicodeError):
        return []


def terms(text):
    words = re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]+', text.lower())
    return {token for word in words for token in ([word] if not '\u4e00' <= word[0] <= '\u9fff' else [word[i:i+2] for i in range(len(word)-1)])}


def relevant(messages, query, limit=6000):
    keys = terms(query)
    if not keys: return ''
    candidates = []
    for index, m in enumerate(messages):
        # Rank short bounded excerpts and prefer recent messages on ties.
        for offset in range(0, min(len(m['text']), 16000), 1200):
            text = m['text'][offset:offset+1200]
            score = len(keys & terms(text))
            if score: candidates.append((score, index, m['role'], text))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected, size = [], 0
    for _, index, role, text in candidates[:6]:
        item = f'[{index+1} {role}] {text}'
        if size + len(item) > limit: continue
        selected.append(item); size += len(item)
    return '\n\n'.join(selected)


def recent_user_turns(messages):
    turns = []
    for m in reversed(messages):
        if m['role'] != 'user': continue
        try: stamp = datetime.fromisoformat(m['timestamp'].replace('Z','+00:00')).timestamp()
        except (ValueError, TypeError, KeyError, AttributeError): continue
        turns.append({'startedAt':stamp, 'pocketSource':'已导入聊天', 'items':[{'type':'userMessage','content':[{'type':'text','text':m['text']}]}]})
        if len(turns) == 6: break
    return turns
