import copy
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import tomlkit
from codex_budget import budget as b
from codex_budget import integration as i


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'budget data'
        self.codex = self.root / 'codex home'
        self.codex.mkdir()
        self.patches = [patch.object(b, 'DATA', self.data),
                        patch.dict(os.environ, {'CODEX_HOME': str(self.codex)})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def hook(self, command='budget hook'):
        return {'type': 'command', 'command': command, 'statusMessage': i.TAG}

    def metadata(self, command):
        return [{'key': str(self.codex / 'hooks.json') + ':' + event + ':1:0',
                 'currentHash': 'sha256:test-' + event, 'command': command}
                for event in i.EVENTS]

    def test_merge_is_idempotent_and_keeps_other_hooks(self):
        other = {'type': 'command', 'command': 'echo other'}
        original = {'hooks': {'UserPromptSubmit': [{'hooks': [other]}],
                              'PreToolUse': [{'hooks': [other]}]}, 'description': 'keep'}
        merged = i.merge_hooks(copy.deepcopy(original), 'new hook')
        self.assertEqual(merged, i.merge_hooks(copy.deepcopy(merged), 'new hook'))
        self.assertEqual(merged['hooks']['UserPromptSubmit'][0], original['hooks']['UserPromptSubmit'][0])
        self.assertEqual(merged['hooks']['PreToolUse'], original['hooks']['PreToolUse'])
        self.assertEqual(merged['description'], 'keep')
        for event in i.EVENTS:
            owned = [h for g in merged['hooks'][event] for h in g['hooks'] if i.owned(h)]
            self.assertEqual(len(owned), 1)

    def test_upgrade_replaces_own_command_in_place(self):
        content = {'hooks': {'Stop': [{'hooks': [self.hook('old hook')]}]}}
        result = i.merge_hooks(content, 'new hook')
        self.assertEqual(result['hooks']['Stop'][0]['hooks'][0]['command'], 'new hook')
        self.assertEqual(len(result['hooks']['Stop']), 1)

    def test_remove_preserves_later_group_indices(self):
        other = {'type': 'command', 'command': 'echo other'}
        content = {'hooks': {'Stop': [{'hooks': [self.hook()]}, {'hooks': [other]}]}}
        result = i.remove_hooks(content)
        self.assertEqual(result['hooks']['Stop'], [{'hooks': []}, {'hooks': [other]}])

    def test_trust_updates_preserve_comments_and_other_settings(self):
        path = self.codex / 'config.toml'
        path.write_text('# Keep me\nmodel = "existing-model"\n[hooks.state.other]\nenabled = false\n')
        hooks = [{'key': 'owned.key', 'currentHash': 'sha256:first'}]
        i.write_trust(hooks)
        i.write_trust([{'key': 'owned.key', 'currentHash': 'sha256:second'}])
        text = path.read_text()
        doc = tomlkit.parse(text)
        self.assertIn('# Keep me', text)
        self.assertEqual(doc['model'], 'existing-model')
        self.assertFalse(doc['hooks']['state']['other']['enabled'])
        self.assertEqual(doc['hooks']['state']['owned.key']['trusted_hash'], 'sha256:second')
        i.write_trust(hooks, remove=True)
        doc = tomlkit.parse(path.read_text())
        self.assertNotIn('owned.key', doc['hooks']['state'])
        self.assertIn('other', doc['hooks']['state'])

    def test_setup_and_uninstall_keep_user_data(self):
        original = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo existing'}]}]}}
        b.write_json(self.codex / 'hooks.json', original)
        with patch.object(b, 'codex_binary', return_value='/some/bin/codex'), patch.object(i, 'metadata', side_effect=self.metadata):
            i.setup(trust=True, service=False)
            first = b.read_json(self.codex / 'hooks.json', {})
            i.setup(trust=True, service=False)
            self.assertEqual(first, b.read_json(self.codex / 'hooks.json', {}))
        b.write_json(self.data / 'state.json', {'preserve': True})
        with patch.object(i, 'service_path', return_value=self.root / 'missing.plist'):
            i.uninstall()
            i.uninstall()
        self.assertEqual(b.read_json(self.data / 'state.json', {}), {'preserve': True})
        self.assertTrue((self.data / 'config.json').exists())
        result = b.read_json(self.codex / 'hooks.json', {})
        self.assertEqual(result['hooks']['Stop'][0], original['hooks']['Stop'][0])
        self.assertFalse(any(i.owned(h) for groups in result['hooks'].values() for g in groups for h in g['hooks']))

    def test_service_uses_installed_python_and_explicit_environment(self):
        import plistlib
        path = self.root / 'LaunchAgents/sample.plist'
        self.data.mkdir()
        with patch.object(i.sys, 'platform', 'darwin'), patch.object(i, 'service_path', return_value=path), patch.object(i.subprocess, 'run') as run:
            i.install_service('/custom/codex')
            spec = plistlib.loads(path.read_bytes())
            self.assertEqual(spec['ProgramArguments'], i.package_command('sample'))
            self.assertEqual(spec['EnvironmentVariables']['CODEX_BINARY'], '/custom/codex')
            self.assertEqual(spec['EnvironmentVariables']['CODEX_BUDGET_HOME'], str(self.data))
            self.assertEqual(spec['StartInterval'], 60)
            self.assertEqual(run.call_args.args[0][1], 'bootstrap')

    def test_python_path_with_spaces_is_shell_quoted(self):
        with patch.object(i.sys, 'executable', '/some python/bin/python'):
            command = shlex.join(i.package_command('hook'))
            self.assertEqual(shlex.split(command), ['/some python/bin/python', '-m', 'codex_budget', 'hook'])

    def test_metadata_matches_canonical_hook_paths(self):
        alias = self.root / 'codex alias'
        alias.symlink_to(self.codex, target_is_directory=True)
        hook = {'statusMessage': i.TAG, 'sourcePath': str((self.codex / 'hooks.json').resolve())}
        with patch.dict(os.environ, {'CODEX_HOME': str(alias)}), patch.object(b, 'rpc', return_value={'data': [{'hooks': [hook]}]}):
            self.assertEqual(i.metadata('unused'), [hook])

    def test_binary_path_falls_back_to_saved_runtime(self):
        b.write_json(self.data / 'runtime.json', {'codex_binary': str(Path(i.sys.executable))})
        with patch.dict(os.environ, {'CODEX_BINARY': ''}), patch.object(b.shutil, 'which', return_value=None):
            self.assertEqual(b.codex_binary(), str(Path(i.sys.executable).absolute()))


if __name__ == '__main__':
    unittest.main()
