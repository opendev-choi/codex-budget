"""Terminal choices independent of Codex's permission approval policy."""
from __future__ import annotations

import curses
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import budget as b

CHOICES = ('5%p 추가 사용', '오늘 제한 해제', '오늘은 쉬기')
TIMEOUT = 60


def available():
    return bool(os.environ.get('TMUX') and os.environ.get('TMUX_PANE'))


def ask(status):
    tmux = os.environ.get('CODEX_BUDGET_TMUX_BINARY') or shutil.which('tmux')
    if not tmux:
        raise RuntimeError('tmux 실행 파일을 찾지 못했습니다.')
    b.DATA.mkdir(parents=True, exist_ok=True)
    # Each request owns its private answer file; simultaneous sessions cannot share choices.
    with tempfile.TemporaryDirectory(prefix='choice-', dir=b.DATA) as folder:
        path = Path(folder) / 'answer.json'
        command = shlex.join([sys.executable, '-m', 'codex_budget.popup', str(path), b.line(status)])
        result = subprocess.run([tmux, 'display-popup', '-E', '-t', os.environ['TMUX_PANE'],
                                 '-w', '76', '-h', '14', '-T', 'Codex budget', command],
                                capture_output=True, text=True, timeout=TIMEOUT + 5)
        if result.returncode:
            raise RuntimeError('예산 팝업을 열지 못했습니다. 연결된 tmux 터미널에서 다시 요청하세요.')
        answer = b.read_json(path, {}).get('choice')
        return answer if answer in CHOICES else None


def select(screen, summary):
    curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(200)
    selected = 2  # Enter alone never grants extra budget.
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        screen.erase()
        rows = [summary, '', '오늘 예산에 도달했습니다. 계속 사용할까요?', '']
        rows += [(' > ' if n == selected else '   ') + f'{n+1}. {choice}' for n, choice in enumerate(CHOICES)]
        rows += ['', '↑↓ / 1·2·3 선택 · Enter 확인 · Esc 취소',
                 f'{max(0, int(deadline-time.monotonic()))}초 후 선택 없이 닫힙니다.']
        height, width = screen.getmaxyx()
        for row, value in enumerate(rows[:max(0, height-1)]):
            try:
                screen.addstr(row, 1, value[:max(0, width//2-2)])
            except curses.error:
                pass
        screen.refresh()
        key = screen.getch()
        if key in (27, 3):
            return None
        if key in (curses.KEY_UP, ord('k')):
            selected = (selected - 1) % len(CHOICES)
        elif key in (curses.KEY_DOWN, ord('j')):
            selected = (selected + 1) % len(CHOICES)
        elif key in (ord('1'), ord('2'), ord('3')):
            selected = key - ord('1')
        elif key in (10, 13, curses.KEY_ENTER):
            return CHOICES[selected]
    return None


if __name__ == '__main__':
    answer = curses.wrapper(select, sys.argv[2])
    if answer is not None:
        b.write_json(Path(sys.argv[1]), {'choice': answer})
