import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_budget import budget as b
from codex_budget import mcp_server as m


class NativeChoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(b, 'DATA', Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.status = {'date': '2026-09-15', 'account': 'test', 'used': 20, 'limit': 20,
                       'weekly_remaining': 60, 'blocked': True, 'enabled': True,
                       'rest': False, 'unlimited': False}

    def answer(self, choice):
        return SimpleNamespace(action='accept', data=m.BudgetChoice(choice=choice))

    async def test_add_budget_continues_original_request(self):
        allowed = {**self.status, 'limit': 25, 'blocked': False}
        ask = AsyncMock(return_value=self.answer('5%p 추가 사용'))
        with patch.object(b, 'refresh', side_effect=[self.status, self.status, allowed]) as refresh:
            result = await m.evaluate('original request', ask)
        self.assertNotIn('decision', result)
        self.assertEqual(refresh.call_args.kwargs['action'], ('allow', '5'))
        self.assertIn('계속', result['systemMessage'])

    async def test_unlimited_is_only_granted_after_explicit_answer(self):
        allowed = {**self.status, 'unlimited': True, 'blocked': False}
        with patch.object(b, 'refresh', side_effect=[self.status, self.status, allowed]) as refresh:
            result = await m.evaluate('request', AsyncMock(return_value=self.answer('오늘 제한 해제')))
        self.assertNotIn('decision', result)
        self.assertEqual(refresh.call_args.kwargs['action'], ('unlimited',))

    async def test_rest_blocks_request(self):
        with patch.object(b, 'refresh', side_effect=[self.status, self.status, {**self.status, 'rest': True}]) as refresh:
            result = await m.evaluate('request', AsyncMock(return_value=self.answer('오늘은 쉬기')))
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(refresh.call_args.kwargs['action'], ('rest',))

    async def test_cancel_and_decline_never_grant_budget(self):
        for action in ('cancel', 'decline'):
            with patch.object(b, 'refresh', return_value=self.status) as refresh:
                result = await m.evaluate('request', AsyncMock(return_value=SimpleNamespace(action=action, data=None)))
            self.assertEqual(result['decision'], 'block')
            self.assertEqual(refresh.call_count, 1)
            self.assertIn('자동 거절', result['reason'])
            self.assertIn('예산 +5', result['reason'])

    async def test_date_or_account_change_during_selection_requires_retry(self):
        for changed in ({'date': '2026-09-16'}, {'account': 'different'}):
            with patch.object(b, 'refresh', side_effect=[self.status, {**self.status, **changed}]) as refresh:
                result = await m.evaluate('request', AsyncMock(return_value=self.answer('오늘 제한 해제')))
            self.assertEqual(result['decision'], 'block')
            self.assertEqual(refresh.call_count, 2)

    async def test_below_budget_warns_without_prompt(self):
        ask = AsyncMock()
        with patch.object(b, 'refresh', return_value={**self.status, 'blocked': False, 'warning': 'warning'}):
            result = await m.evaluate('request', ask)
        self.assertEqual(result, {'systemMessage': 'warning'})
        ask.assert_not_called()

    async def test_failed_form_blocks_request(self):
        with patch.object(b, 'refresh', return_value=self.status):
            result = await m.evaluate('request', AsyncMock(side_effect=RuntimeError('form unavailable')))
        self.assertEqual(result['decision'], 'block')

    async def test_disabled_does_not_query_or_ask(self):
        b.write_json(b.DATA / 'config.json', {'enabled': False})
        with patch.object(b, 'refresh') as refresh:
            result = await m.evaluate('request', AsyncMock())
        self.assertEqual(result, {})
        refresh.assert_not_called()

    def test_choices_have_no_preapproved_default(self):
        from mcp.server.elicitation import _validate_elicitation_schema
        _validate_elicitation_schema(m.BudgetChoice)
        schema = m.BudgetChoice.model_json_schema()
        self.assertEqual(set(schema), {'type', 'properties', 'required'})
        self.assertEqual(schema['required'], ['choice'])
        self.assertNotIn('default', schema['properties']['choice'])
        self.assertEqual(len(schema['properties']['choice']['enum']), 3)
