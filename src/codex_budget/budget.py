from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import holidays

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CODEX_BUDGET_HOME', Path.home() / '.config/codex-budget'))
def codex_binary():
    configured = os.environ.get('CODEX_BINARY')
    runtime = read_json(DATA / 'runtime.json', {})
    binary = configured or shutil.which('codex') or runtime.get('codex_binary')
    if not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError('Codex CLI를 PATH에 설치하거나 CODEX_BINARY를 지정하세요.')
    return str(Path(binary).absolute())

DEFAULTS = {'daily_percent': 'auto', 'holidays': 'include', 'weekends': 'include',
            'timezone': 'Asia/Seoul', 'bucket': 'codex', 'enabled': True,
            'extra_holidays': {}, 'working_dates': [], 'warn_at': [25, 50, 75, 90]}


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.budget-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def locked():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / 'state.lock').open('a') as handle:
        end = time.monotonic() + 18
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    raise RuntimeError('예산 집계 잠금 시간 초과. 다시 시도해 주세요.')
                time.sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def rpc(method, params=None):
    # A separate read-only app-server avoids depending on a shared daemon socket.
    process = subprocess.Popen([codex_binary(), 'app-server', '--stdio'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b''
    deadline = time.monotonic() + 12
    def send(value):
        process.stdin.write((json.dumps(value) + '\n').encode())
        process.stdin.flush()
    send({'id': 1, 'method': 'initialize', 'params': {
        'clientInfo': {'name': 'codex_budget', 'version': '1.0'},
        'capabilities': {'experimentalApi': True}}})
    try:
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline-time.monotonic())):
                break
            part = os.read(process.stdout.fileno(), 65536)
            if not part:
                raise RuntimeError('Codex 사용량 조회 프로세스가 종료됐습니다.')
            buffer += part
            while b'\n' in buffer:
                raw, buffer = buffer.split(b'\n', 1)
                message = json.loads(raw)
                if message.get('id') not in (1, 2):
                    continue
                if 'error' in message:
                    raise RuntimeError('Codex 조회 실패: ' + str(message['error'].get('message', 'unknown')))
                if message['id'] == 1:
                    send({'method': 'initialized', 'params': {}})
                    request = {'id': 2, 'method': method}
                    if params is not None:
                        request['params'] = params
                    send(request)
                else:
                    return message['result']
        raise RuntimeError('Codex 사용량 조회가 12초 안에 완료되지 않았습니다.')
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


def weekly_snapshot(result, bucket):
    limits = result.get('rateLimitsByLimitId') or {}
    entry = limits.get(bucket)
    if entry is None and (result.get('rateLimits') or {}).get('limitId') == bucket:
        entry = result['rateLimits']
    for key in ('primary', 'secondary'):
        window = (entry or {}).get(key) or {}
        if window.get('windowDurationMins') == 10080:
            used, reset = window.get('usedPercent'), window.get('resetsAt')
            if not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                raise RuntimeError('주간 사용률 값이 올바르지 않습니다.')
            if not isinstance(reset, int) or reset <= time.time():
                raise RuntimeError('주간 리셋 정보가 만료되었습니다. 잠시 후 다시 시도해 주세요.')
            account = result.get('accountId')
            if not account:
                raise RuntimeError('계정 구분 정보가 없어 사용량을 안전하게 집계할 수 없습니다.')
            return {'used': used, 'reset': reset, 'account': hashlib.sha256(account.encode()).hexdigest()[:20]}
    raise RuntimeError(f'{bucket}의 7일 한도가 없습니다. 선택한 계정/모델 한도를 확인해 주세요.')


def eligible(day, config):
    if day.isoformat() in config['working_dates']:
        return True
    if config['weekends'] == 'exclude' and day.weekday() >= 5:
        return False
    if config['holidays'] == 'exclude':
        calendar = holidays.KR(years=day.year, language='ko', observed=True)
        if day in calendar or day.isoformat() in config['extra_holidays']:
            return False
    return True


def remaining_days(now, reset, config):
    last = dt.datetime.fromtimestamp(reset, now.tzinfo)
    day = now.date()
    count = 0
    # A reset exactly at midnight does not allocate a pre-reset budget to that day.
    while dt.datetime.combine(day, dt.time(), now.tzinfo) < last:
        count += int(eligible(day, config))
        day += dt.timedelta(days=1)
    return count


def account_sample(state, snap, now):
    date = now.date().isoformat()
    key = snap['account']
    accounts = state.setdefault('accounts', {})
    account = accounts.setdefault(key, {'days': {}})
    day = account['days'].setdefault(date, {'used': 0.0, 'extra': 0.0, 'unlimited': False, 'rest': False})
    old = account.get('last')
    # The live endpoint's reset timestamp can jitter by a second between reads.
    reset_changed = old is not None and abs(old['reset'] - snap['reset']) > 300
    if old is None:
        day['partial'] = True
        delta = 0
    elif reset_changed:
        # Any usage already present in the new weekly window was consumed since reset.
        delta = snap['used']
    else:
        # Preserve the high water mark if the service briefly reports a smaller value.
        delta = max(0, snap['used'] - old['used'])
        snap = {**snap, 'used': max(snap['used'], old['used'])}
    day['used'] += delta
    if old and old['date'] != date and delta:
        day['boundary_estimate'] = True
    if reset_changed:
        day.pop('auto', None)
    account['last'] = {**snap, 'at': now.timestamp(), 'date': date}
    for key_to_remove in sorted(account['days'])[:-35]:
        del account['days'][key_to_remove]
    state['active_account'] = key
    return day, account['last']


def build_status(day, snap, config, now):
    allowed = eligible(now.date(), config)
    fingerprint = json.dumps({k: config[k] for k in ('daily_percent', 'holidays', 'weekends', 'extra_holidays', 'working_dates')}, sort_keys=True)
    if config['daily_percent'] == 'auto':
        auto = day.get('auto')
        if auto is None or auto['config'] != fingerprint or abs(auto['reset'] - snap['reset']) > 300:
            slots = remaining_days(now, snap['reset'], config)
            # Reconstruct the remaining quota at the start of this local day.
            budget = (day['used'] + max(0, 100 - snap['used'])) / max(1, slots)
            auto = day['auto'] = {'budget': budget, 'config': fingerprint, 'reset': snap['reset'], 'days': slots}
        base = auto['budget'] if allowed else 0.0
    else:
        base = config['daily_percent'] if allowed else 0.0
    limit = base + day['extra']
    blocked = bool(config['enabled'] and not day['unlimited'] and (day['rest'] or day['used'] + 1e-9 >= limit))
    progress = day['used'] / limit * 100 if limit > 0 else 0.0
    return {'date': now.date().isoformat(), 'used': day['used'], 'limit': limit,
            'account': snap.get('account'), 'progress_percent': progress, 'warn_at': config.get('warn_at', DEFAULTS['warn_at']),
            'weekly_remaining': max(0, 100-snap['used']), 'reset': snap['reset'],
            'blocked': blocked, 'enabled': config['enabled'], 'rest': day['rest'],
            'unlimited': day['unlimited'], 'eligible': allowed, 'partial': day.get('partial', False),
            'boundary_estimate': day.get('boundary_estimate', False), 'sampled_at': snap['at'],
            'holidays': config['holidays'], 'weekends': config['weekends'],
            'mode': config['daily_percent'], 'timezone': config['timezone']}


def change_day(day, action):
    if action[0] == 'allow':
        value = percent(action[1])
        if value <= 0:
            raise ValueError('추가 사용량은 0보다 커야 합니다.')
        day['extra'] += value
        day['rest'] = False
    elif action[0] == 'unlimited':
        day['unlimited'], day['rest'] = True, False
    elif action[0] == 'rest':
        day['rest'], day['unlimited'] = True, False
    elif action[0] == 'resume':
        day['rest'], day['unlimited'] = False, False
        day['extra'] = 0


def warning_thresholds(value):
    if value.strip().lower() == 'off':
        return []
    levels = sorted({percent(part.strip()) for part in value.split(',')})
    if not levels or levels[0] <= 0:
        raise ValueError('경고 구간은 0 초과 100 이하의 숫자 목록 또는 off여야 합니다.')
    return levels


def take_warning(day, status, consume=False):
    if not status['enabled'] or status['unlimited'] or status['rest'] or status['blocked']:
        return None
    levels = status['warn_at']
    scope = json.dumps([round(status['limit'], 8), levels])
    history = day.setdefault('warning_history', {})
    seen = history.get(scope, [])
    reached = [level for level in levels if status['progress_percent'] + 1e-9 >= level]
    pending = [level for level in reached if level not in seen]
    if not pending:
        return None
    if consume:
        history[scope] = sorted(set(seen + reached))
    level = max(pending)
    return (f"일일 예산 {level:g}% 경고: 현재 {status['progress_percent']:.1f}% 사용 "
            f"({status['used']:.1f}/{status['limit']:.1f}%p). "
            f"주간 잔여 {status['weekly_remaining']:.0f}%. 계속 사용할 수 있습니다.")


def refresh(fresh=False, action=None, consume_warnings=False):
    with locked():
        config = {**DEFAULTS, **read_json(DATA / 'config.json', {})}
        state = read_json(DATA / 'state.json', {})
        now = dt.datetime.now(ZoneInfo(config['timezone']))
        previous = (state.get('accounts', {}).get(state.get('active_account'), {}) or {}).get('last')
        if not fresh and previous and 0 <= now.timestamp()-previous['at'] < 15 and previous['date'] == now.date().isoformat() and previous['reset'] > now.timestamp():
            snap = previous
            day = state['accounts'][state['active_account']]['days'][snap['date']]
        else:
            snap = weekly_snapshot(rpc('account/rateLimits/read'), config['bucket'])
            day, snap = account_sample(state, snap, now)
        if action:
            change_day(day, action)
        status = build_status(day, snap, config, now)
        status['warning'] = take_warning(day, status, consume=consume_warnings)
        state['status'] = status
        write_json(DATA / 'state.json', state)
        return status


def line(status):
    badge = 'OFF' if not status['enabled'] else ('쉬기' if status['rest'] else ('오늘 해제' if status['unlimited'] else ('초과' if status['blocked'] else '사용 중')))
    reached = [level for level in status.get('warn_at', []) if status.get('progress_percent', 0) >= level]
    if badge == '사용 중' and reached:
        badge = f'경고 {max(reached):g}% · 예산 {status["progress_percent"]:.0f}%'
    return f"오늘 {status['used']:.1f}/{status['limit']:.1f}%p | 주간 잔여 {status['weekly_remaining']:.0f}% | {badge}"


def notice(status):
    return (line(status) + '\n오늘 예산에 도달했거나 쉬는 날입니다. 지원되는 실행 모드에서는 다음 요청에 선택 창이 표시됩니다. 직접 입력도 가능:\n'
            '  예산 +5  → 오늘 5%p 추가\n  예산 해제  → 오늘 제한 해제\n  예산 쉬기  → 오늘 쉬기\n'
            '직접 입력했다면 원래 요청을 다시 보내세요. 설정: 다른 셸에서 codex-budget configure')


def percent(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError('퍼센트는 0~100 사이의 유한한 숫자여야 합니다.')
    return result


def prompt_action(prompt):
    words = prompt.strip().split()
    if not words or words[0] not in ('예산', 'budget'):
        return None
    if len(words) == 1 or words[1:] in (['상태'], ['status']):
        return ('status',)
    if len(words) == 2:
        if words[1].startswith('+'):
            percent(words[1][1:])
            return ('allow', words[1][1:])
        options = {'해제': 'unlimited', 'unlimited': 'unlimited', '쉬기': 'rest', 'rest': 'rest', '복귀': 'resume', 'resume': 'resume'}
        if words[1] in options:
            return (options[words[1]],)
    raise ValueError('사용법: 예산 상태 / 예산 +5 / 예산 해제 / 예산 쉬기 / 예산 복귀')


def run_hook():
    event = json.load(sys.stdin)
    name = event.get('hook_event_name')
    try:
        action = prompt_action(event.get('prompt', '')) if name == 'UserPromptSubmit' else None
        config = {**DEFAULTS, **read_json(DATA / 'config.json', {})}
        if not config['enabled'] and action is None:
            print('{}')
            return
        status = refresh(fresh=name in ('UserPromptSubmit', 'Stop'),
                         action=action if action and action[0] != 'status' else None,
                         consume_warnings=action is None)
        if action:
            result = {'decision': 'block', 'reason': line(status) + '\n선택을 적용했습니다. 원래 요청을 다시 입력하세요.'}
        elif name == 'UserPromptSubmit' and status['blocked']:
            result = {'decision': 'block', 'reason': notice(status)}
        elif name == 'SessionStart':
            result = {'systemMessage': notice(status) if status['blocked'] else (status.get('warning') or line(status))}
        elif name == 'Stop' and status['blocked']:
            result = {'continue': False, 'stopReason': notice(status)}
        elif status.get('warning'):
            result = {'systemMessage': status['warning']}
        else:
            result = {}
    except Exception as exc:
        message = f'예산 확인 실패: {exc}\n다시 시도하거나 다른 셸에서 codex-budget disable 로 제한을 해제하세요.'
        result = {'decision': 'block', 'reason': message} if name == 'UserPromptSubmit' else {'systemMessage': message}
    print(json.dumps(result, ensure_ascii=False))


def show(status):
    print(line(status))
    reset = dt.datetime.fromtimestamp(status['reset'], ZoneInfo(status['timezone']))
    print(f"주간 리셋: {reset:%Y-%m-%d %H:%M} ({status['timezone']})")
    print(f"일일 설정: {status['mode']} · 공휴일 {status['holidays']} · 주말 {status['weekends']}")
    print('경고 구간 (일일 예산 대비): ' + (', '.join(f'{x:g}%' for x in status.get('warn_at', [])) or 'off'))
    if status['partial']:
        print('오늘은 설치 후 첫 조회부터 집계합니다. 설치 전 사용량은 일일 집계에 포함되지 않습니다.')
    if status['boundary_estimate']:
        print('자정 사이 조회 간격의 사용량은 새 날짜에 합산한 추정치입니다.')


def configure(args):
    with locked():
        config = {**DEFAULTS, **read_json(DATA / 'config.json', {})}
        if args.warn_at is not None:
            config['warn_at'] = warning_thresholds(args.warn_at)
        if args.daily_percent is not None:
            config['daily_percent'] = 'auto' if args.daily_percent == 'auto' else percent(args.daily_percent)
        for key in ('holidays', 'weekends'):
            value = getattr(args, key)
            if value is not None:
                config[key] = value
        if args.extra_holiday:
            date, label = args.extra_holiday
            dt.date.fromisoformat(date)
            config['extra_holidays'][date] = label
        if args.working_date:
            dt.date.fromisoformat(args.working_date)
            if args.working_date not in config['working_dates']:
                config['working_dates'].append(args.working_date)
        write_json(DATA / 'config.json', config)
    print(json.dumps(config, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='Codex 주간 사용량을 일일 예산으로 관리합니다. %는 주간 총한도의 %p입니다.')
    parser.add_argument('--version', action='version', version='%(prog)s 0.1.0')
    sub = parser.add_subparsers(dest='command')
    setup_parser = sub.add_parser('setup', help='Codex 훅과 macOS 사용량 조회 서비스 설치')
    setup_parser.add_argument('--trust-hooks', action='store_true', help='이 패키지의 훅 정의를 신뢰하도록 등록')
    setup_parser.add_argument('--no-service', action='store_true', help='macOS 백그라운드 조회 생략')
    sub.add_parser('uninstall', help='이 도구의 훅과 조회 서비스만 제거; 집계/설정은 보존')
    sub.add_parser('doctor', help='CLI, 사용량 조회, 훅 상태 확인')
    for name in ('status', 'sample', 'line', 'hook', 'unlimited', 'rest', 'resume', 'disable', 'enable'):
        sub.add_parser(name)
    allow = sub.add_parser('allow'); allow.add_argument('percent')
    conf = sub.add_parser('configure')
    conf.add_argument('--daily-percent', help='auto 또는 0~100 (주간 총한도 대비)')
    conf.add_argument('--warn-at', help='일일 예산 대비 경고 구간: 25,50,75,90 또는 off')
    conf.add_argument('--holidays', choices=['include', 'exclude'])
    conf.add_argument('--weekends', choices=['include', 'exclude'])
    conf.add_argument('--extra-holiday', nargs=2, metavar=('YYYY-MM-DD', 'NAME'))
    conf.add_argument('--working-date', metavar='YYYY-MM-DD')
    args = parser.parse_args()
    if args.command in ('setup', 'uninstall', 'doctor'):
        from . import integration
        if args.command == 'setup':
            integration.setup(trust=args.trust_hooks, service=not args.no_service)
        elif args.command == 'uninstall':
            integration.uninstall()
        else:
            integration.doctor()
        return
    if args.command == 'hook':
        run_hook(); return
    if args.command == 'line':
        status = read_json(DATA / 'state.json', {}).get('status')
        if not status:
            print('Codex 예산: 집계 대기'); return
        text = line(status)
        if time.time()-status['sampled_at'] > 150:
            text += ' | 조회 지연'
        print(text); return
    if args.command == 'configure':
        configure(args); return
    if args.command in ('enable', 'disable'):
        with locked():
            config = {**DEFAULTS, **read_json(DATA / 'config.json', {})}
            config['enabled'] = args.command == 'enable'
            write_json(DATA / 'config.json', config)
        print('예산 제한 ' + ('활성화' if config['enabled'] else '해제'))
        return
    action = None
    if args.command in ('allow', 'unlimited', 'rest', 'resume'):
        action = (args.command, args.percent) if args.command == 'allow' else (args.command,)
    status = refresh(fresh=True, action=action)
    if args.command != 'sample':
        show(status)


def entrypoint():
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'codex-budget: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    entrypoint()
