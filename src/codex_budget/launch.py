from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time

from . import budget as b
from .integration import atomic_text, package_command


def main():
    tmux = shutil.which('tmux')
    if not tmux:
        raise RuntimeError('tmux가 필요합니다. macOS: brew install tmux')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('codex-budget-run은 대화형 터미널에서 실행하세요.')
    binary = b.codex_binary()
    subprocess.run(package_command('sample'), check=False, timeout=35)
    b.DATA.mkdir(parents=True, exist_ok=True)
    render = shlex.join(['env', 'CODEX_BUDGET_HOME=' + str(b.DATA), *package_command('line')])
    # Encode the whole shell command as a tmux string, including spaces in Python's path.
    quoted = ('#(' + render + ')').replace('\\', '\\\\').replace('"', '\\"')
    config = b.DATA / 'tmux.conf'
    atomic_text(config, '\n'.join([
        'set -g status on', 'set -g status-interval 5', "set -g status-left ''",
        'set -g status-right-length 180', f'set -g status-right "{quoted}"',
        "set -g status-style 'fg=colour231,bg=colour24'", 'set -g history-limit 20000',
        'set -g mouse on', '',
    ]))
    session = f'budget-{os.getpid()}-{int(time.time())}'
    command = 'exec ' + shlex.join([binary, *sys.argv[1:]])
    base = [tmux, '-L', 'codex-budget', '-f', str(config)]
    subprocess.run([*base, 'new-session', '-d', '-s', session, '-c', os.getcwd(),
                    '-e', 'CODEX_BUDGET_HOME=' + str(b.DATA), command], check=True)
    subprocess.run([*base, 'source-file', str(config)], check=True)
    env = os.environ.copy()
    env.pop('TMUX', None)
    os.execve(tmux, [*base, 'attach-session', '-t', session], env)


def entrypoint():
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'codex-budget-run: {exc}', file=sys.stderr)
        sys.exit(1)
