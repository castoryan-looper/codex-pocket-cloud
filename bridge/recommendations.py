"""Small, local recommendation feed grounded in recent device conversations."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
import unicodedata


@dataclass(frozen=True)
class Card:
    id: str
    topic: str
    title: str
    prompt: str
    strategy: str
    source: str
    detail: str = ""
    context: str = ""


STRATEGIES = (
    ('advance', '推进下一步', '先核对已经完成的工作，再选一个最值得推进的小步骤，给出具体方案并在现有权限内完成。不要重复已完成的操作。'),
    ('verify', '找出一个隐患', '针对这个话题，检查最可能遗漏的边界或失败场景，给出验证办法和优先修复建议。先分析，不直接更改或部署。'),
    ('explain', '换个角度理解', '围绕这个话题，挑一个最值得理解的原理，用直观例子说明，并列出两种可选方案的取舍。先讨论，不执行修改。'),
    ('experiment', '设计一个小实验', '围绕这个话题，设计一个十分钟左右可以验证的小实验，说明目标、步骤和判断标准。先给计划，等待我决定是否执行。'),
)


def recent_topics(turns, now):
    result, seen = [], set()
    if not isinstance(turns, list):
        return result
    for turn in turns[:12]:
        if not isinstance(turn, dict): continue
        started = turn.get('startedAt')
        if isinstance(started, bool) or not isinstance(started, (int, float)) or not -300 <= now-started <= 604800:
            continue
        items = turn.get('items')
        if not isinstance(items, list): continue
        for item in items:
            if not isinstance(item, dict) or item.get('type') != 'userMessage': continue
            parts = item.get('content')
            if not isinstance(parts, list): continue
            raw = ' '.join(p['text'] for p in parts if isinstance(p, dict) and p.get('type') == 'text' and isinstance(p.get('text'), str))
            raw = re.sub(r'<(environment_context|in-app-browser-context|external_codex_apps_open_page)\b[^>]*>.*?</\1>', '', raw, flags=re.S)
            raw = ' '.join(raw.split())[:1800]
            if len(raw) < 4: continue
            fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:16]
            if fingerprint in seen: continue
            seen.add(fingerprint)
            # Keep the user's actual wording, rather than replacing it with a broad keyword category.
            title = re.split(r'[。！？!?\n]', raw)[0]
            title = re.sub(r'^(?:请|帮我|我想|我希望|能不能|可以|关于|我们来|现在)+', '', title).strip(' ，,：:')
            if not title: title = raw
            result.append((fingerprint, title[:48], raw, '已导入聊天' if turn.get('pocketSource')=='已导入聊天' else '设备近期聊天'))
    return result


class Feed:
    def __init__(self, path):
        self.path = Path(path)
        self.topics = []
        self.feedback = {'seen': [], 'used': [], 'weights': {}}
        try:
            data = json.loads(self.path.read_text('utf-8'))
            if isinstance(data, dict):
                self.feedback['seen'] = [x for x in data.get('seen', []) if isinstance(x, str)][-100:]
                self.feedback['used'] = [x for x in data.get('used', []) if isinstance(x, str)][-100:]
                self.feedback['weights'] = {k: max(-5, min(5, v)) for k, v in data.get('weights', {}).items()
                                            if k in {s[0] for s in STRATEGIES} and isinstance(v, int)}
        except (OSError, ValueError, TypeError, AttributeError): pass

    def update(self, turns, now=None):
        self.topics = recent_topics(turns, time.time() if now is None else now)

    def save(self):
        tmp = self.path.with_suffix('.tmp')
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(self.feedback, stream)
            tmp.replace(self.path)
        except OSError:
            pass  # A feedback disk error cannot make a submitted task appear to fail.

    def pair(self):
        candidates = []
        for rank, (fingerprint, topic, raw, source) in enumerate(self.topics):
            for order, (strategy, title, instruction) in enumerate(STRATEGIES):
                ident = fingerprint + ':' + strategy
                if ident in self.feedback['used']: continue
                prompt = f'我们最近讨论过：\n「{raw}」\n\n这次我选择：{title}。\n{instruction}\n请结合本会话最新进展；如果缺少信息，先提出具体问题。'
                card = Card(ident, topic, f'{title}：{topic}', prompt, strategy, source, f'话题：{topic}\n\n{instruction}', raw)
                score = 100-rank*5-order + self.feedback['weights'].get(strategy, 0)*2
                if ident in self.feedback['seen']: score -= 80
                candidates.append((score, card))
        if not candidates and self.topics:
            cards = []
            for index, strategy in enumerate(('explain', 'verify')):
                fingerprint, topic, raw, source = self.topics[min(index,len(self.topics)-1)]
                instruction = STRATEGIES[2 if index==0 else 1][2]
                detail = '话题：'+topic+'\n\n此前的建议已浏览过，这次换成回顾：'+instruction
                cards.append(Card(fingerprint+':revisit:'+strategy, topic, '回顾与取舍：'+topic, '最近我们讨论过：'+raw+'\n'+detail, strategy, source+'·回顾', detail, raw))
            return tuple(cards)
        if not candidates:
            return tuple(Card('starter:'+s, '先告诉我你关心什么', t, p, s, '起步建议', p) for s, t, p in (
                ('advance', '聊聊当前项目的下一步', '请先问我目前正在做什么项目、卡在哪里，再一起选一个值得推进的小步骤。'),
                ('explain', '探索一个我感兴趣的新话题', '请先问我最近对什么感兴趣，再给我两个具体的探索方向；不要假设你知道我的兴趣。')))
        candidates.sort(key=lambda x: x[0], reverse=True)
        first = candidates[0][1]
        # Offer a different recent topic when possible, otherwise a different angle on the same one.
        second = next((c for _, c in candidates if c.topic != first.topic and c.strategy != first.strategy), None)
        second = second or next((c for _, c in candidates if c.id != first.id), None)
        if second is None:
            second = Card(first.id+':reflect', first.topic, '回顾这个话题的取舍', '请回顾我们关于「'+first.topic+'」的讨论，列出两种方案的取舍。先讨论，不执行修改。', 'explain', '近期聊天', '回顾「'+first.topic+'」的讨论，列出两种方案的取舍。先讨论，不执行修改。', first.context)
        return first, second

    def record(self, cards, chosen=None):
        for card in cards:
            self.feedback['seen'] = [x for x in self.feedback['seen'] if x != card.id] + [card.id]
        self.feedback['seen'] = self.feedback['seen'][-100:]
        if chosen:
            self.feedback['used'] = (self.feedback['used'] + [chosen.id])[-100:]
            self.feedback['weights'][chosen.strategy] = min(5, self.feedback['weights'].get(chosen.strategy, 0)+1)
        self.save()


def paginate(text, measure, width=184, rows=5):
    """Pixel-width pagination without truncation or automatic scrolling."""
    lines, current = [], ''
    for char in str(text).replace('\r', ''):
        if char == '\n':
            lines.append(current); current = ''; continue
        if current and not unicodedata.combining(char) and measure(current+char) > width:
            lines.append(current); current = ''
        current += char
    if current or not lines: lines.append(current)
    return [lines[i:i+rows] for i in range(0, len(lines), rows)]
