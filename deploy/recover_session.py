"""Back up settings and detach only a verified missing hardware session."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def detach_missing_session(state_dir: Path, codex_home: Path, expected: str):
    settings_path = state_dir / 'settings.json'
    original = settings_path.read_bytes()
    settings = json.loads(original)
    actual = settings.get('thread_id')
    if actual != expected:
        raise ValueError('保存的会话 ID 已变化；没有修改配置，请重新检查网关日志。')
    existing = []
    for directory in (codex_home / 'sessions', codex_home / 'archived_sessions'):
        if directory.is_dir():
            existing.extend(directory.rglob(f'*{expected}*.jsonl'))
    if existing:
        raise ValueError('找到了可能属于该会话的本地记录；没有修改配置，请先检查这些记录。')
    backup = settings_path.with_name(f'settings.before-session-recovery-{time.time_ns()}.json')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(original)
        output.flush()
        os.fsync(output.fileno())
    del settings['thread_id']
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=state_dir, prefix='.session-recovery-', delete=False) as output:
            temp_path = Path(output.name)
            json.dump(settings, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, settings_path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return backup


def main():
    if len(sys.argv) != 2:
        raise SystemExit('请提供日志中无法恢复的会话 ID。')
    try:
        backup = detach_missing_session(Path(os.environ['POCKET_STATE_DIR']), Path(os.environ['CODEX_HOME']), sys.argv[1])
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print('配置已备份：' + str(backup))
    print('已移除失效会话的引用；下次启动将创建新的独立硬件助手会话。')


if __name__ == '__main__':
    main()
