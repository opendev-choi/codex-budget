from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

import tomlkit

from . import budget as b

TAG = 'codex-budget'
EVENTS = ('SessionStart', 'UserPromptSubmit', 'Stop')
LABEL = 'local.codex-budget.sample'


def codex_home():
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser()


def package_command(action):
    return [sys.executable, '-m', 'codex_budget', action]


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.codex-budget-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def backup(path):
    if path.exists():
        folder = b.DATA / 'backups'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(path, folder / f'{path.name}.{stamp}')


def owned(handler):
    legacy = str(Path.home() / '.local/bin/codex-budget') + ' hook'
    return handler.get('statusMessage') == TAG or handler.get('command') == legacy


def handler_for(event, command):
    if event == 'UserPromptSubmit':
        return {'type': 'mcp_tool', 'server': TAG, 'tool': 'budget_gate',
                'input': {'prompt': '${prompt}'}, 'timeout': 120, 'statusMessage': TAG}
    return {'type': 'command', 'command': command, 'timeout': 45, 'statusMessage': TAG}


def merge_hooks(content, command):
    for event in EVENTS:
        groups = content.setdefault('hooks', {}).setdefault(event, [])
        inserted = False
        for group in groups:
            handlers = group.get('hooks', [])
            replacement = []
            for handler in handlers:
                if owned(handler):
                    if not inserted:
                        replacement.append(handler_for(event, command))
                        inserted = True
                else:
                    replacement.append(handler)
            group['hooks'] = replacement
        if not inserted:
            groups.append({'hooks': [handler_for(event, command)]})
    return content


def remove_hooks(content):
    # Empty groups preserve the numeric trust keys of later, unrelated hooks.
    for event in EVENTS:
        for group in content.get('hooks', {}).get(event, []):
            group['hooks'] = [handler for handler in group.get('hooks', []) if not owned(handler)]
    return content


def metadata(command):
    response = b.rpc('hooks/list', {'cwds': [str(b.DATA)]})
    hooks_path = (codex_home() / 'hooks.json').resolve()
    return [hook for entry in response['data'] for hook in entry['hooks']
            if hook.get('statusMessage') == TAG and Path(hook['sourcePath']).resolve() == hooks_path]


def configure_mcp(remove=False):
    path = codex_home() / 'config.toml'
    original = path.read_text() if path.exists() else ''
    doc = tomlkit.parse(original)
    servers = doc.setdefault('mcp_servers', {})
    existing = servers.get(TAG)
    if existing and list(existing.get('args', [])) != ['-m', 'codex_budget.mcp_server']:
        raise RuntimeError('codex-budget 이름의 다른 MCP 서버가 있습니다. 이름 충돌을 먼저 해결하세요.')
    if remove:
        servers.pop(TAG, None)
    else:
        servers[TAG] = {'command': sys.executable, 'args': ['-m', 'codex_budget.mcp_server'],
                        'env': {'CODEX_BINARY': b.codex_binary(), 'CODEX_BUDGET_HOME': str(b.DATA),
                                'CODEX_HOME': str(codex_home())},
                        'env_vars': ['TMUX', 'TMUX_PANE'],
                        'required': True, 'startup_timeout_sec': 20, 'tool_timeout_sec': 150}
    if not remove and shutil.which('tmux'):
        servers[TAG]['env']['CODEX_BUDGET_TMUX_BINARY'] = shutil.which('tmux')
    updated = tomlkit.dumps(doc)
    if updated != original:
        backup(path)
        if (path.read_text() if path.exists() else '') != original:
            raise RuntimeError('Codex 설정이 동시에 변경됐습니다. 다시 실행하세요.')
        atomic_text(path, updated)


def write_trust(hooks, remove=False):
    path = codex_home() / 'config.toml'
    original = path.read_text() if path.exists() else ''
    doc = tomlkit.parse(original)
    state = doc.setdefault('hooks', {}).setdefault('state', {})
    for hook in hooks:
        key = hook['key']
        if remove:
            state.pop(key, None)
        else:
            state[key] = {'trusted_hash': hook['currentHash'], 'enabled': True}
    updated = tomlkit.dumps(doc)
    if updated != original:
        backup(path)
        if (path.read_text() if path.exists() else '') != original:
            raise RuntimeError('Codex 설정이 동시에 변경됐습니다. 다시 실행하세요.')
        atomic_text(path, updated)


def service_path():
    return Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'


def install_service(binary):
    if sys.platform != 'darwin':
        raise RuntimeError('백그라운드 조회는 macOS 전용입니다. setup --no-service를 사용하세요.')
    path = service_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup(path)
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
    spec = {'Label': LABEL, 'ProgramArguments': package_command('sample'),
            'StartInterval': 60, 'RunAtLoad': True, 'ProcessType': 'Background',
            'WorkingDirectory': str(b.DATA),
            'EnvironmentVariables': {'CODEX_BINARY': binary, 'CODEX_BUDGET_HOME': str(b.DATA),
                                     'CODEX_HOME': str(codex_home())},
            'StandardErrorPath': str(b.DATA / 'sampler-error.log')}
    atomic_text(path, plistlib.dumps(spec).decode())
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)


def setup(trust=False, service=True):
    if service and sys.platform != 'darwin':
        raise RuntimeError('백그라운드 조회는 macOS 전용입니다. setup --no-service를 사용하세요.')
    binary = b.codex_binary()
    command = shlex.join(package_command('hook'))
    with b.locked():
        b.write_json(b.DATA / 'runtime.json', {'codex_binary': binary})
        if not (b.DATA / 'config.json').exists():
            b.write_json(b.DATA / 'config.json', b.DEFAULTS)
        path = codex_home() / 'hooks.json'
        content = b.read_json(path, {'hooks': {}})
        backup(path)
        b.write_json(path, merge_hooks(content, command))
        configure_mcp()
        hooks = metadata(command)
        if len(hooks) != len(EVENTS):
            raise RuntimeError('이 Codex CLI에서 예산 훅 3개를 확인하지 못했습니다. CLI를 업데이트하세요.')
        if trust:
            write_trust(hooks)
        b.write_json(b.DATA / 'installation.json', {'command': command, 'hooks': hooks,
                                                    'codex_home': str(codex_home())})
        if service:
            install_service(binary)
    print('설치 완료. Codex CLI를 다시 시작하세요.')
    if not trust:
        print('Codex에서 /hooks를 열고 codex-budget 훅 3개를 검토·신뢰해야 제한이 작동합니다.')
    print('확인: codex-budget doctor  |  상태줄: codex-budget-run')


def uninstall():
    with b.locked():
        manifest = b.read_json(b.DATA / 'installation.json', {})
        if manifest.get('codex_home') and manifest['codex_home'] != str(codex_home()):
            raise RuntimeError('설치 당시 CODEX_HOME으로 다시 실행하세요.')
        path = codex_home() / 'hooks.json'
        if path.exists():
            content = b.read_json(path, {})
            backup(path)
            b.write_json(path, remove_hooks(content))
        if manifest:
            configure_mcp(remove=True)
        if manifest.get('hooks'):
            write_trust(manifest['hooks'], remove=True)
        path = service_path()
        if sys.platform == 'darwin' and path.exists():
            subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
            path.unlink()
        (b.DATA / 'installation.json').unlink(missing_ok=True)
    print('예산 훅·조회 서비스를 제거했습니다. 설정과 사용량 기록은 보존했습니다.')


def doctor():
    binary = b.codex_binary()
    print(subprocess.check_output([binary, '--version'], text=True).strip())
    manifest = b.read_json(b.DATA / 'installation.json', {})
    hooks = metadata(manifest.get('command', shlex.join(package_command('hook'))))
    ready = len(hooks) == 3 and all(hook['enabled'] and hook['trustStatus'] == 'trusted' for hook in hooks)
    for hook in hooks:
        print(f"{hook['eventName']}: {hook['trustStatus']}, enabled={hook['enabled']}")
    b.show(b.refresh(fresh=True))
    print('tmux: ' + (shutil.which('tmux') or '미설치 (상태줄 실행에만 필요)'))
    if sys.platform == 'darwin':
        result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
        print('백그라운드 조회: ' + ('등록됨' if result.returncode == 0 else '미등록'))
    if not ready:
        raise RuntimeError('예산 훅이 준비되지 않았습니다. setup 후 Codex /hooks에서 신뢰 상태를 확인하세요.')
