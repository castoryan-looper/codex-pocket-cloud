"""Validated, persistent settings for the dedicated Voxi voice sessions."""
import copy
import json
import os
from pathlib import Path
from voxi_voices import validate_voice, catalog

VOICE_TASK_INSTRUCTIONS = (
    'You are a voice assistant with read-only web research. Use web search for current facts, '
    'including weather, and keep the same thread for follow-up questions. Complete the research '
    'and return a concise spoken answer with the actual result. Never stop at a promise to look '
    'something up. Ask for missing locations or dates. If research fails, explain the failure. '
    'For weather, return only conditions, temperature and any essential warning in one or two '
    'sentences. Omit hourly lists, repeated location/date, introductions, summaries and follow-up '
    'offers unless the user explicitly requests a detailed forecast. '
    'Do not run shell commands, modify files, or perform external actions.'
)

RESEARCH_PROMPT = (
    '\n需要天气、新闻或其他实时信息时，将查询委托给 Codex 后端，取得结果后主动继续播报，'
    '不用等用户再问。过程说明不代表任务完成。沿用同一会话中的城市、日期和查询结果回答追问。'
    '缺少必要信息先询问；查询失败要明确说明，不编造结果。'
    '\n天气查询默认只播报天气、温度和必要提醒，控制在一到两句。不要逐时罗列，'
    '不重复城市和日期，不加开场白、总结或追问。用户要求详细预报时再展开。'
)

DEFAULTS = {
    'voice':'cove',
    'systemPrompt': '你是虚构对讲机里的AI伙伴。默认用简短自然中文直接回应，通常一到三句。用户明确要求讲故事、详细讲解或长回复时，按要求完整讲完，不受十秒或句数限制；不要为了简短而截断内容。偶尔说收到或完毕，不要每句重复。不要模拟电流声。听不清就请求重说。身份被问及时如实说明是AI。',
    'channels': [
        {'name': '营地一号', 'persona': '沉稳、熟悉山野的营地值守员。关心对方，但不说教。'},
        {'name': '巡逻二号', 'persona': '干练的巡逻搭档。观察敏锐，回答直接，有一点幽默。'},
        {'name': '夜航三号', 'persona': '深夜值班的老朋友。安静、放松，像隔着无线电陪伴。'},
        {'name': '探索四号', 'persona': '好奇、务实的探索队友。一起解决问题。'},
    ],
    'responseIdleTimeoutSeconds': 30,
    'replyEndSilenceMs': 1000,
    'mediaStatsIntervalSeconds': 10,
    'audioRetransmission': True,
    'audioRecoveryPackets': 64,
}

def validate(value):
    if isinstance(value,dict) and set(value)==set(DEFAULTS)-{'voice'}:value={**value,'voice':DEFAULTS['voice']}
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('配置字段不完整或包含不支持的字段')
    validate_voice(value['voice'])
    def text(value, length):
        if not isinstance(value, str) or not value.strip() or len(value) > length or '\x00' in value:
            raise ValueError(f'文本不能为空，最多 {length} 字符')
    text(value['systemPrompt'], 16000)
    channels = value['channels']
    if not isinstance(channels, list) or len(channels) != 4:
        raise ValueError('必须提供四个频道')
    for channel in channels:
        if not isinstance(channel, dict) or set(channel) != {'name', 'persona'}:
            raise ValueError('频道字段无效')
        text(channel['name'], 64)
        text(channel['persona'], 4000)
    for key, low, high in [('responseIdleTimeoutSeconds', 10, 180), ('replyEndSilenceMs', 500, 5000), ('mediaStatsIntervalSeconds', 5, 120)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'{key} 必须为 {low}–{high} 的整数')
    if type(value['audioRetransmission']) is not bool or type(value['audioRecoveryPackets']) is not int or value['audioRecoveryPackets'] not in (16,32,64,128):
        raise ValueError('音频重传或恢复窗口配置无效')
    return copy.deepcopy(value)

def effective_prompt(settings, channel):
    selected = settings['channels'][channel]
    # Migrate the exact old built-in restriction for already-saved configurations.
    prompt = settings['systemPrompt'].replace('不要执行任务或使用工具。', '')
    return prompt + '\n当前频道呼号：' + selected['name'] + '。\n频道角色：' + selected['persona'] + RESEARCH_PROMPT

class VoiceSettings:
    def __init__(self, directory=None):
        self.path = Path(directory) / 'voxi-config.json' if directory else None
        self.value = copy.deepcopy(DEFAULTS)
        if self.path and self.path.exists():
            self.value = validate(json.loads(self.path.read_text(encoding='utf-8')))

    def snapshot(self):
        return copy.deepcopy(self.value)

    def save(self, value):
        if isinstance(value,dict) and 'voice' not in value:value={**value,'voice':self.value['voice']}
        value = validate(value)
        if self.path:
            temporary = self.path.with_suffix('.tmp')
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        self.value = value

    def public(self):
        return {'settings': self.snapshot(), 'defaults': copy.deepcopy(DEFAULTS),
                'effectivePrompts': [effective_prompt(self.value, i) for i in range(4)],
                'voices':catalog(self.value['voice']),
                'runtime': {'voice': self.value['voice'], 'protocol': 'v3', 'model': '由 Codex 原生语音后端选择',
                            'developerInstructions': VOICE_TASK_INSTRUCTIONS},
                'persistent': self.path is not None,
                'applies': '保存后在新连接或切换频道时生效；当前故事不会被打断。'}
