import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from codex_budget import budget as b

TZ = ZoneInfo('Asia/Seoul')

class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.config = {**b.DEFAULTS, 'extra_holidays': {}, 'working_dates': []}
        self.now = dt.datetime(2027, 1, 4, 10, tzinfo=TZ)
        self.reset = int(dt.datetime(2027, 1, 11, 0, tzinfo=TZ).timestamp())

    def snap(self, used, reset=None, account='a'):
        return {'used': used, 'reset': reset or self.reset, 'account': account}

    def test_primary_and_secondary_weekly_windows(self):
        weekly = {'usedPercent': 82, 'windowDurationMins': 10080, 'resetsAt': self.reset}
        for field in ['primary', 'secondary']:
            result = {'accountId': 'a', 'rateLimitsByLimitId': {'codex': {field: weekly}}}
            with patch.object(b.time, 'time', return_value=self.now.timestamp()):
                self.assertEqual(b.weekly_snapshot(result, 'codex')['used'], 82)

    def test_no_weekly_bucket_does_not_guess(self):
        with self.assertRaises(RuntimeError):
            b.weekly_snapshot({'accountId': 'a', 'rateLimits': {'limitId': 'codex', 'primary': {'windowDurationMins': 300}}}, 'codex')

    def test_first_sample_then_delta_and_duplicate(self):
        state = {}
        day, _ = b.account_sample(state, self.snap(82), self.now)
        self.assertEqual(day['used'], 0)
        self.assertTrue(day['partial'])
        day, _ = b.account_sample(state, self.snap(85), self.now)
        self.assertEqual(day['used'], 3)
        b.account_sample(state, self.snap(85), self.now)
        b.account_sample(state, self.snap(84), self.now)
        b.account_sample(state, self.snap(85), self.now)
        self.assertEqual(day['used'], 3)

    def test_midnight_does_not_reset_weekly_baseline(self):
        state = {}
        b.account_sample(state, self.snap(82), self.now)
        day, _ = b.account_sample(state, self.snap(85), self.now+dt.timedelta(days=1))
        self.assertEqual(day['used'], 3)
        self.assertTrue(day['boundary_estimate'])
        self.assertEqual(day['extra'], 0)

    def test_weekly_reset_keeps_same_day_usage(self):
        state = {}
        day, _ = b.account_sample(state, self.snap(82), self.now)
        b.account_sample(state, self.snap(85), self.now)
        day, _ = b.account_sample(state, self.snap(2, self.reset+604800), self.now)
        self.assertEqual(day['used'], 5)

    def test_reset_timestamp_jitter_does_not_count_weekly_usage_again(self):
        state = {}
        day, snap = b.account_sample(state, self.snap(82), self.now)
        original = b.build_status(day, snap, self.config, self.now)['limit']
        day, snap = b.account_sample(state, self.snap(83, self.reset-1), self.now)
        self.assertEqual(day['used'], 1)
        self.assertEqual(b.build_status(day, snap, self.config, self.now)['limit'], original)
        day, snap = b.account_sample(state, self.snap(83, self.reset+1), self.now)
        self.assertEqual(day['used'], 1)

    def test_accounts_are_isolated(self):
        state = {}
        b.account_sample(state, self.snap(82), self.now)
        day, _ = b.account_sample(state, self.snap(10, account='b'), self.now)
        self.assertEqual(day['used'], 0)
        day, _ = b.account_sample(state, self.snap(84), self.now)
        self.assertEqual(day['used'], 2)

    def test_auto_remaining_distribution_is_stable_through_day(self):
        state = {}
        day, snap = b.account_sample(state, self.snap(79), self.now)
        initial = b.build_status(day, snap, self.config, self.now)
        self.assertEqual(initial['limit'], 3)
        day, snap = b.account_sample(state, self.snap(82), self.now)
        status = b.build_status(day, snap, self.config, self.now)
        self.assertEqual(status['limit'], 3)
        self.assertTrue(status['blocked'])
        b.change_day(day, ('allow', '5'))
        status = b.build_status(day, snap, self.config, self.now)
        self.assertEqual(status['limit'], 8)
        self.assertFalse(status['blocked'])

    def test_holidays_weekends_and_overrides(self):
        self.config.update(holidays='exclude', weekends='include')
        self.assertFalse(b.eligible(dt.date(2027, 1, 1), self.config))
        self.assertTrue(b.eligible(dt.date(2027, 1, 2), self.config))
        self.config['weekends'] = 'exclude'
        self.assertFalse(b.eligible(dt.date(2027, 1, 2), self.config))
        self.config['working_dates'] = ['2027-01-01']
        self.assertTrue(b.eligible(dt.date(2027, 1, 1), self.config))
        self.config['extra_holidays']['2027-01-04'] = '휴가'
        self.assertFalse(b.eligible(self.now.date(), self.config))

    def test_actual_substitute_holiday(self):
        self.config['holidays'] = 'exclude'
        self.assertFalse(b.eligible(dt.date(2026, 3, 2), self.config))

    def test_exclusion_changes_auto_divisor_and_fixed_budget(self):
        self.config['weekends'] = 'exclude'
        state = {}
        day, snap = b.account_sample(state, self.snap(80), self.now)
        self.assertEqual(b.build_status(day, snap, self.config, self.now)['limit'], 4)
        self.config['daily_percent'] = 17
        self.assertEqual(b.build_status(day, snap, self.config, self.now)['limit'], 17)
        saturday = dt.datetime(2027, 1, 9, 10, tzinfo=TZ)
        status = b.build_status(day, snap, self.config, saturday)
        self.assertEqual(status['limit'], 0)
        self.assertTrue(status['blocked'])

    def test_rest_unlimited_resume_and_daily_reset(self):
        state = {}
        day, snap = b.account_sample(state, self.snap(80), self.now)
        b.change_day(day, ('rest',))
        self.assertTrue(b.build_status(day, snap, self.config, self.now)['blocked'])
        b.change_day(day, ('unlimited',))
        self.assertFalse(b.build_status(day, snap, self.config, self.now)['blocked'])
        b.change_day(day, ('resume',))
        self.assertFalse(day['unlimited'])
        b.change_day(day, ('unlimited',))
        day, _ = b.account_sample(state, self.snap(80), self.now+dt.timedelta(days=1))
        self.assertFalse(day['unlimited'])

    def test_invalid_percent(self):
        for value in ['nan', 'inf', '-1', '101']:
            with self.assertRaises(ValueError): b.percent(value)
        self.assertEqual(b.percent('14.2857'), 14.2857)

    def hook(self, event, status=None, error=None):
        inp, out = io.StringIO(json.dumps(event)), io.StringIO()
        with tempfile.TemporaryDirectory() as temp, patch.object(b, 'DATA', Path(temp)), patch.object(b.sys, 'stdin', inp), contextlib.redirect_stdout(out), patch.object(b, 'refresh', return_value=status, side_effect=error):
            b.run_hook()
        return json.loads(out.getvalue())

    def test_prompt_blocks_before_model_and_offers_choices(self):
        day, snap = b.account_sample({}, self.snap(80), self.now)
        self.config['daily_percent'] = 0
        status = b.build_status(day, snap, self.config, self.now)
        output = self.hook({'hook_event_name': 'UserPromptSubmit', 'prompt': '코드 수정해줘'}, status)
        self.assertEqual(output['decision'], 'block')
        self.assertIn('예산 +5', output['reason'])
        output = self.hook({'hook_event_name': 'Stop'}, status)
        self.assertFalse(output['continue'])
        self.assertIn('stopReason', output)
        self.assertNotIn('systemMessage', output)

    def test_query_failure_blocks_prompt(self):
        output = self.hook({'hook_event_name': 'UserPromptSubmit', 'prompt': '작업'}, error=RuntimeError('offline'))
        self.assertEqual(output['decision'], 'block')
        self.assertIn('확인 실패', output['reason'])

    def test_override_prompt_is_consumed_locally(self):
        self.assertEqual(b.prompt_action('예산 +5'), ('allow', '5'))
        self.assertEqual(b.prompt_action('예산 해제'), ('unlimited',))
        self.assertIsNone(b.prompt_action('테스트 해줘'))
        day, snap = b.account_sample({}, self.snap(80), self.now)
        output = self.hook({'hook_event_name': 'UserPromptSubmit', 'prompt': '예산 상태'}, b.build_status(day, snap, self.config, self.now))
        self.assertEqual(output['decision'], 'block')

    def warning_status(self, used=0, limit=20):
        day, snap = b.account_sample({}, self.snap(80), self.now)
        day['used'] = used
        config = {**self.config, 'daily_percent': limit, 'warn_at': [25, 50, 75, 90]}
        return day, b.build_status(day, snap, config, self.now)

    def test_warning_threshold_configuration(self):
        self.assertEqual(b.warning_thresholds('50,25,50, 62.5'), [25, 50, 62.5])
        self.assertEqual(b.warning_thresholds('off'), [])
        for value in ['0,50', '101', 'nan', '', '25,']:
            with self.assertRaises(ValueError): b.warning_thresholds(value)

    def test_warning_measures_daily_budget_not_weekly_usage(self):
        day, status = self.warning_status(5, 20)
        self.assertEqual(status['progress_percent'], 25)
        self.assertIn('25% 경고', b.take_warning(day, status, consume=True))
        self.assertIn('경고 25%', b.line(status))

    def test_warning_jump_coalesces_and_deduplicates(self):
        day, status = self.warning_status(15, 20)
        self.assertIn('75% 경고', b.take_warning(day, status, consume=True))
        self.assertIsNone(b.take_warning(day, status, consume=True))
        status['progress_percent'] = 90
        self.assertIn('90% 경고', b.take_warning(day, status, consume=True))
        self.assertIsNone(b.take_warning(day, status, consume=True))

    def test_sampler_does_not_consume_cli_warning(self):
        day, status = self.warning_status(10, 20)
        self.assertIsNotNone(b.take_warning(day, status))
        self.assertIsNotNone(b.take_warning(day, status))
        self.assertIsNotNone(b.take_warning(day, status, consume=True))
        self.assertIsNone(b.take_warning(day, status))

    def test_warning_suppressed_when_unlimited_disabled_or_blocked(self):
        day, status = self.warning_status(15, 20)
        for change in [{'unlimited': True}, {'enabled': False}, {'blocked': True}, {'rest': True}, {'warn_at': []}]:
            self.assertIsNone(b.take_warning(day, {**status, **change}, consume=True))

    def test_warning_history_resets_for_next_day_and_new_limit(self):
        day, status = self.warning_status(10, 20)
        b.take_warning(day, status, consume=True)
        self.assertIsNotNone(b.take_warning({}, status, consume=True))
        changed = {**status, 'limit': 40, 'progress_percent': 25}
        self.assertIn('25% 경고', b.take_warning(day, changed, consume=True))
        self.assertIsNone(b.take_warning(day, status, consume=True))

    def test_warning_does_not_block_normal_prompt(self):
        day, status = self.warning_status(10, 20)
        status['warning'] = b.take_warning(day, status, consume=True)
        result = self.hook({'hook_event_name': 'UserPromptSubmit', 'prompt': '코드 수정해줘'}, status)
        self.assertIn('50% 경고', result['systemMessage'])
        self.assertNotIn('decision', result)

if __name__ == '__main__':
    unittest.main()
