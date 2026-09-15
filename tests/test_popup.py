import curses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_budget import budget as b
from codex_budget import popup as p


class PopupTest(unittest.TestCase):
    def select(self, keys):
        screen = Mock()
        screen.getmaxyx.return_value = (14, 76)
        screen.getch.side_effect = keys
        with patch.object(p.curses, 'curs_set'):
            return p.select(screen, '오늘 예산')

    def test_enter_defaults_to_rest(self):
        self.assertEqual(self.select([10]), '오늘은 쉬기')

    def test_add_budget_requires_selection_and_confirmation(self):
        self.assertEqual(self.select([ord('1'), 10]), '5%p 추가 사용')
        self.assertIsNone(self.select([ord('1'), 27]))

    def test_arrow_selection(self):
        self.assertEqual(self.select([curses.KEY_UP, 10]), '오늘 제한 해제')

    def test_timeout_never_grants_budget(self):
        with patch.object(p.time, 'monotonic', side_effect=[0, p.TIMEOUT+1]), patch.object(p.curses, 'curs_set'):
            self.assertIsNone(p.select(Mock(), 'budget'))

    def test_popup_targets_own_pane_and_reads_only_its_answer(self):
        with tempfile.TemporaryDirectory(prefix='popup test ') as folder, patch.object(b, 'DATA', Path(folder)), patch.dict(p.os.environ, {'TMUX_PANE': '%42', 'CODEX_BUDGET_TMUX_BINARY': '/tmux'}):
            def run(args, **kwargs):
                import shlex
                command = shlex.split(args[-1])
                self.assertEqual(args[args.index('-t')+1], '%42')
                b.write_json(Path(command[3]), {'choice': '5%p 추가 사용'})
                return Mock(returncode=0)
            with patch.object(p.subprocess, 'run', side_effect=run), patch.object(b, 'line', return_value='budget'):
                self.assertEqual(p.ask({}), '5%p 추가 사용')
            self.assertEqual(list(Path(folder).iterdir()), [])
