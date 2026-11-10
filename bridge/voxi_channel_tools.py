"""Built-in workflows and channel tool validation."""
import re

IMPORTED_SKILL_ID = re.compile(r"skill-[a-f0-9]{32}")
GITHUB_ID = "github-readonly"

SKILLS = {
    'research': ('资料调研', '列出证据、出处和仍待确认的结论。',
                 '先明确问题和范围。优先核对一手资料和日期，交叉验证关键结论；最后区分已证实信息与推断，附可核对的来源。'),
    'code-review': ('代码审查', '检查缺陷、回归和测试覆盖，给出定位。',
                    '先读取目标代码、差异和相关测试。按影响排序报告具体问题并标注文件位置，区分已验证问题和疑点。只有用户明确要求时才修改代码。'),
    'file-organizing': ('文件整理', '先盘点，再给出可核对的整理方案。',
                        '先做只读盘点，列出建议分类、命名规则和可能冲突。移动、覆盖或删除前取得用户明确授权，操作后核对结果。'),
}

MCP_SERVERS = {
    'openai-docs': ('OpenAI 官方文档', '只读搜索与读取 OpenAI 开发者文档。查询内容会发送给文档服务。',
                    'voxi_openai_docs', 'https://developers.openai.com/mcp',
                    ('search_openai_docs', 'fetch_openai_doc')),
}
ROLE_LIMIT = 1500


def channel_tools_catalog():
    return {
        'roleLimit': ROLE_LIMIT,
        'skills': [{'id': key, 'name': name, 'description': description}
                   for key, (name, description, _) in SKILLS.items()],
        'mcp': [{'id': key, 'name': name, 'description': description}
                for key, (name, description, _, _, _) in MCP_SERVERS.items()],
    }


def validate_channel_personalization(channel):
    role = channel.get('role', '')
    if not isinstance(role, str) or len(role) > ROLE_LIMIT:
        raise ValueError(f'频道角色最多 {ROLE_LIMIT} 个字符')
    role = role.strip()
    result = {}
    if role:
        if channel['mode'] not in ('fast', 'agent'):
            raise ValueError('仅快问和 Voxi Agent 支持自定义角色')
        result['role'] = role
    for field, catalog in (('skills', SKILLS), ('mcp', MCP_SERVERS)):
        items = channel.get(field, [])
        if (not isinstance(items, list) or len(items) > 16
                or any(not isinstance(item, str) or not (item in catalog or
                       (field == 'skills' and IMPORTED_SKILL_ID.fullmatch(item)) or
                       (field == 'mcp' and item == GITHUB_ID)) for item in items)
                or len(items) != len(set(items))):
            raise ValueError('请选择 Voxi 提供的 Skill 或 MCP 工具')
        if items:
            if channel['mode'] != 'agent':
                raise ValueError('仅 Voxi Agent 支持 Skill 和 MCP 工具')
            result[field] = sorted(items)
    return result


def agent_personalization(route, imported=None):
    notes = []
    if route.get('role'):
        notes.append('用户为当前频道设置的角色与工作偏好：\n' + route['role'])
    for key in route.get('skills', []):
        if key in SKILLS:
            name, _, instructions = SKILLS[key]
            notes.append(f'可按任务需要使用工作流程「{name}」：{instructions}')
        else:
            item = (imported or {})[key]
            resources = '\n\n'.join(path+'\n'+text for path,text in item['references'].items())
            notes.append('用户启用的 Skill：'+item['name']+'\n来源：'+item['source']+'\n'+item['instructions']+'\n'+resources)
    return '\n\n'.join(notes)


def codex_mcp_config(selected):
    return {name: {'url': url, 'enabled': key in selected, 'required': key in selected,
                   'enabled_tools': list(tools), 'startup_timeout_sec': 8}
            for key, (_, _, name, url, tools) in MCP_SERVERS.items()}
