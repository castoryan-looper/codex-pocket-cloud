"""Voices supported by the pinned Codex 0.163.0 realtime V3 protocol.

Source: openai/codex rust-v0.163.0, RealtimeVoicesList::builtin().v3.
Catalog compatibility does not assert account entitlement; preview uses real audio.
"""
VOICES=('cove','juniper','maple','spruce','ember','vale','breeze','arbor','sol',
        'aube','bubbie','haetsal','hanul','himari','leher','miko','neer','porto',
        'rindo','rio','rivage','selva','sonna','tinta','viola')
PREVIEW_TEXT="你好，我是 Voxi。很高兴和你一起，把事情做好。 Hello, I'm Voxi. Let's make good things happen together."
LANGUAGE_NOTE='每段先说中文，再说英文。当前音色没有官方逐项语言适配评分，请以双语样音判断口音和自然度。'


def validate_voice(value, inherit=False):
    if not isinstance(value,str) or (value not in VOICES and not (inherit and value=='')):
        raise ValueError('请选择当前 Codex v3 支持的音色')
    return value


def catalog(default='cove', selected=''):
    return {'defaultVoice':default,'selectedVoice':selected,'effectiveVoice':selected or default,
            'options':[{'id':v,'name':v.title(),'previewLanguages':['zh-CN','en'],'languageAssessment':'unrated'} for v in VOICES],
            'previewText':PREVIEW_TEXT,'languageNote':LANGUAGE_NOTE,'protocol':'v3','previewVersion':2}
