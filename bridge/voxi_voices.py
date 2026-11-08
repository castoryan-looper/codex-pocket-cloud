"""Voices supported by the pinned Codex 0.163.0 realtime V3 protocol.

Source: openai/codex rust-v0.163.0, RealtimeVoicesList::builtin().v3.
Catalog compatibility does not assert account entitlement; preview uses real audio.
"""
VOICES=('cove','juniper','maple','spruce','ember','vale','breeze','arbor','sol',
        'aube','bubbie','haetsal','hanul','himari','leher','miko','neer','porto',
        'rindo','rio','rivage','selva','sonna','tinta','viola')
PREVIEW_SEGMENTS={'zh-CN':'你好，很高兴认识你。祝你今天心情愉快。',
                  'en':'Hello, nice to meet you. Have a wonderful day.'}
PREVIEW_TEXT=' '.join(PREVIEW_SEGMENTS.values())
LANGUAGE_NOTE='中文、英文分别生成并转写核对，选项只标注已通过核对的样音语言；这不代表口音与自然度评级。'
STYLE_SOURCE='https://help.openai.com/en/articles/20001274-chatgpt-voice'
# Product help describes these voices, but does not specify gender/pitch.
STYLES={'cove':('沉稳直接','工作答疑、清晰播报'), 'juniper':('开朗有活力','日常交流、轻松陪伴'),
        'maple':('明快坦率','轻松聊天、创意交流'), 'spruce':('平静、给人支持感','耐心解释、安静陪伴'),
        'ember':('自信积极','行动提醒、日常鼓励'), 'vale':('明亮、富有好奇心','探索新知、知识问答'),
        'breeze':('生动真诚','活泼互动、轻松聊天'), 'arbor':('随和多变','日常交谈、多种话题'),
        'sol':('轻松机敏','随意交流、思路讨论')}
LOCALES={'aube':'法语','rivage':'法语','sonna':'德语','miko':'德语','leher':'印地语','neer':'印地语',
         'himari':'日语','rindo':'日语','haetsal':'韩语','hanul':'韩语','tinta':'墨西哥西班牙语',
         'bubbie':'英语','viola':'巴西葡萄牙语','rio':'巴西葡萄牙语'}


def validate_voice(value, inherit=False):
    if not isinstance(value,str) or (value not in VOICES and not (inherit and value=='')):
        raise ValueError('请选择当前 Codex v3 支持的音色')
    return value


def catalog(default='cove', selected=''):
    return {'defaultVoice':default,'selectedVoice':selected,'effectiveVoice':selected or default,
            'options':[{'id':v,'name':v.title(),'style':STYLES.get(v,('声线风格待试听标注',''))[0],
                        'useCase':STYLES.get(v,('',''))[1], 'styleSource':'official-summary' if v in STYLES else 'unreviewed',
                        'sourceUrl':STYLE_SOURCE if v in STYLES or v in LOCALES else '',
                        'languageReference':LOCALES.get(v,''), 'previewLanguages':[], 'languageAssessment':'unrated'} for v in VOICES],
            'previewText':PREVIEW_TEXT,'previewScripts':PREVIEW_SEGMENTS,'languageNote':LANGUAGE_NOTE,'protocol':'v3','previewVersion':4}
