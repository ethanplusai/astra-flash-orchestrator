from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skill' / 'astra-flash-orchestrator' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
import install
from local_config import (
    ROUTE, ROLE, SKILL, SUPPORTED_ROUTES, SetupError, inspect, model_entries,
    resolve_worker_route, resolve_worker_selection, BACKEND_NATIVE,
)
from validate_plan import PlanError, validate


class SetupFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.codex = self.home / '.codex'
        self.codex.mkdir()
        self.config = self.codex / 'config.toml'
        self.config.write_text(
            '# Preserve my exact config and comments.\n'
            'model = "fixture-astra-root"\nmodel_reasoning_effort = "medium"\n'
            'openai_base_url = "http://127.0.0.1:4202/_codex-router/TEST_PRIVATE_CAPABILITY/v1"\n'
            'model_catalog_json = "catalog.json"\n'
            '[model_providers.unused]\nexperimental_bearer_token = "TEST_SECRET_KEY"\n'
        )
        self.catalog = self.codex / 'catalog.json'
        self.catalog.write_text(json.dumps({'models': [{
            'slug': ROUTE, 'multi_agent_version': 'v2', 'default_reasoning_level': 'high',
            'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}]
        }]}))
        self.policy = self.codex / 'AGENTS.md'
        self.policy.write_text('# Existing instructions\nUse one agent by default.\nPreserve my unrelated notes.\n')
        self.original_config = self.config.read_bytes()
        self.original_policy = self.policy.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'install.py'), '--home', str(self.home),
                               '--codex-home', str(self.codex), *args], capture_output=True, text=True)

    def report(self):
        return inspect(self.home, self.codex)[0]

    def set_catalog_route(self, route, multi_agent_version='v2'):
        payload = json.loads(self.catalog.read_text())
        payload['models'][0]['slug'] = route
        payload['models'][0]['multi_agent_version'] = multi_agent_version
        self.catalog.write_text(json.dumps(payload))

    def changes(self, **kwargs):
        return install.plan_changes(self.home, self.codex, self.report(), kwargs.get('policy', True), kwargs.get('replace', False))

    def apply(self):
        report = self.report()
        changes = install.plan_changes(self.home, self.codex, report, True, False)
        return install.apply_changes(changes, self.codex, report['input_hashes'])

    def test_dry_run_changes_nothing_and_redacts_secrets(self):
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn('TEST_PRIVATE_CAPABILITY', result.stdout + result.stderr)
        self.assertNotIn('TEST_SECRET_KEY', result.stdout + result.stderr)

    def test_install_preserves_config_and_adds_narrow_policy(self):
        result = self.cli('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertTrue(self.policy.read_bytes().startswith(self.original_policy))
        self.assertIn(b'scoped exception', self.policy.read_bytes())
        role = tomllib.loads((self.codex / 'agents' / f'{ROLE}.toml').read_text())
        self.assertEqual(role['model'], ROUTE)
        self.assertEqual(role['model_reasoning_effort'], 'high')
        self.assertFalse(role['agents']['enabled'])
        self.assertNotIn('sandbox_mode', role)
        self.assertNotIn('model_provider', role)
        self.assertIn('No model request was made', result.stdout)

    def test_supported_worker_routes_are_selected_explicitly(self):
        for route, provider in SUPPORTED_ROUTES.items():
            with self.subTest(route=route):
                self.set_catalog_route(route)
                report, _ = inspect(self.home, self.codex, worker_route=route)
                self.assertEqual(report['worker_model'], route)
                self.assertEqual(report['worker_provider'], provider)

    def test_openrouter_route_is_pinned_in_role_and_routing_binding(self):
        route = 'openrouter/deepseek-v4.1-flash'
        self.set_catalog_route(route)
        result = self.cli('--worker-route', route, '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        role = tomllib.loads((self.codex / 'agents' / f'{ROLE}.toml').read_text())
        self.assertEqual(role['model'], route)
        routing = json.loads(
            (self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text()
        )
        self.assertEqual(routing['worker_model'], route)
        self.assertEqual(routing['worker_provider'], 'OpenRouter')

    def test_existing_alternate_binding_is_reused_on_update(self):
        route = 'openrouter/deepseek-v4.1-flash'
        self.set_catalog_route(route)
        first = self.cli('--worker-route', route, '--apply')
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli('--apply')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('no changes needed', second.stdout)
        role = tomllib.loads((self.codex / 'agents' / f'{ROLE}.toml').read_text())
        self.assertEqual(role['model'], route)

    def test_default_route_does_not_fall_back_to_available_alternate(self):
        self.set_catalog_route('openrouter/deepseek-v4.1-flash')
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn(ROUTE, result.stderr)
        self.assertIn('No provider was substituted', result.stderr)
        self.assertFalse((self.home / '.agents').exists())

    def test_unreviewed_worker_route_is_rejected(self):
        with self.assertRaisesRegex(SetupError, 'Unsupported worker route'):
            inspect(self.home, self.codex, worker_route='custom/deepseek-v4.1-flash')

    def test_planned_writes_never_include_config(self):
        self.assertNotIn(self.config, {change['path'] for change in self.changes()})

    def test_install_excludes_local_backup_and_cache_artifacts(self):
        source = self.home / 'synthetic-skill'
        source.mkdir()
        for name in ['SKILL.md', 'helper.py', 'helper.py.before-v1-compat', '.DS_Store', 'helper.pyc', 'helper.bak']:
            (source / name).write_text('fixture')
        with patch.object(install, 'SKILL_SOURCE', source):
            changes = self.changes()
        names = {change['path'].name for change in changes}
        self.assertIn('helper.py', names)
        self.assertFalse(names & {'helper.py.before-v1-compat', '.DS_Store', 'helper.pyc', 'helper.bak'})

    def test_install_is_idempotent(self):
        self.apply()
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.cli('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn('no changes needed', result.stdout)

    def test_no_policy_option(self):
        result = self.cli('--apply', '--no-policy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.policy.read_bytes(), self.original_policy)

    def test_existing_override_file_is_the_policy_target(self):
        override = self.codex / 'AGENTS.override.md'
        override.write_text('Keep this active override.\n')
        self.apply()
        self.assertIn(install.BEGIN, override.read_bytes())
        self.assertEqual(self.policy.read_bytes(), self.original_policy)

    def test_install_does_not_require_global_subagent_default(self):
        result = self.cli('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_different_global_subagent_default_is_ignored(self):
        self.config.write_text(self.config.read_text() + '\n[agents]\ndefault_subagent_model = "fixture-other-model"\n')
        original = self.config.read_bytes()
        result = self.cli('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertIn('global default_subagent_model is not used or changed', result.stdout)
        role = tomllib.loads((self.codex / 'agents' / f'{ROLE}.toml').read_text())
        self.assertEqual(role['model'], ROUTE)

    def test_missing_catalog_route_fails(self):
        self.catalog.write_text('{"models": []}')
        with self.assertRaises(SetupError):
            self.report()

    def test_non_spawnable_catalog_route_blocks_install(self):
        payload = json.loads(self.catalog.read_text())
        for value in ['v1', None]:
            with self.subTest(version=value):
                payload['models'][0]['multi_agent_version'] = value
                self.catalog.write_text(json.dumps(payload))
                result = self.cli('--apply')
                self.assertEqual(result.returncode, 2)
                self.assertIn('not advertised for native subagents', result.stderr)
                self.assertFalse((self.home / '.agents').exists())
                self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_non_spawnable_alternate_route_blocks_install_without_paid_probe_advice(self):
        route = 'openrouter/deepseek-v4.1-flash'
        self.set_catalog_route(route, 'v1')
        result = self.cli('--worker-route', route, '--apply')
        self.assertEqual(result.returncode, 2)
        self.assertIn(route, result.stderr)
        self.assertIn('Do not run subagents certify', result.stderr)
        self.assertFalse((self.home / '.agents').exists())

    def test_flash_root_is_rejected_for_every_supported_provider(self):
        route = 'openrouter/deepseek-v4.1-flash'
        self.set_catalog_route(route)
        self.config.write_text(self.config.read_text().replace('fixture-astra-root', route))
        with self.assertRaisesRegex(SetupError, 'root model is Flash'):
            inspect(self.home, self.codex, worker_route=route)

    def test_non_loopback_route_fails_without_exposing_url(self):
        self.config.write_text(self.config.read_text().replace('127.0.0.1', 'private.remote.test'))
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('TEST_PRIVATE_CAPABILITY', result.stdout + result.stderr)

    def test_native_direct_v1_route_is_supported(self):
        self.config.write_text(self.config.read_text().replace('/_codex-router/TEST_PRIVATE_CAPABILITY/v1', '/v1'))
        original = self.config.read_bytes()
        self.assertEqual(self.report()['worker_model'], ROUTE)
        result = self.cli('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), original)

    def test_invalid_router_url_shapes_fail_closed(self):
        original = self.config.read_text()
        valid = 'http://127.0.0.1:4202/_codex-router/TEST_PRIVATE_CAPABILITY/v1'
        for url in ['http://example.invalid/v1', 'http://127.0.0.1:4202/arbitrary/v1',
                    'http://127.0.0.1:4202/v1?secret=TEST_PRIVATE_CAPABILITY',
                    'http://127.0.0.1:4202/v1#secret', 'http://user:secret@127.0.0.1:4202/v1',
                    'file:///v1', 'http://127.0.0.1:4202/_codex-router/a/extra/v1']:
            with self.subTest(url=url):
                self.config.write_text(original.replace(valid, url))
                with self.assertRaises(SetupError):
                    self.report()

    def test_existing_backup_directory_permissions_preserved(self):
        backup = self.codex / 'astra-flash-install-backups'
        backup.mkdir(mode=0o750)
        before = backup.stat().st_mode
        self.apply()
        self.assertEqual(backup.stat().st_mode, before)

    def test_malformed_toml_does_not_echo_secret(self):
        self.config.write_text('token = "TEST_SECRET_KEY\n')
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('TEST_SECRET_KEY', result.stdout + result.stderr)

    def test_disabled_subagents_fail_closed(self):
        self.config.write_text(self.config.read_text() + '\n[agents]\nenabled = false\n')
        with self.assertRaises(SetupError):
            self.report()

    def test_misplaced_top_level_setting_under_agents_gets_actionable_error(self):
        self.config.write_text(
            '# A misplaced table header makes the following URL part of agents.\n'
            '[agents]\ndefault_subagent_model = "fixture-other-model"\n'
            'openai_base_url = "http://127.0.0.1:4202/v1"\n'
            'model_catalog_json = "catalog.json"\n'
        )
        with self.assertRaisesRegex(SetupError, r'do not belong under \[agents\].*openai_base_url'):
            self.report()

    def test_absorbed_key_is_caught_by_shape_not_by_a_known_name_list(self):
        # Regression for a real incident: an agent satisfying an older installer
        # prerequisite appended [agents] to config.toml, which absorbed the two
        # top-level realtime keys that followed it. Codex refused to load the
        # config, taking down the host app and the CLI. Neither key is a
        # plausible member of a list of anticipated top-level names, so the
        # guard has to reject them on shape.
        self.config.write_text(
            'model = "fixture-astra-root"\n'
            'openai_base_url = "http://127.0.0.1:4202/v1"\n'
            'model_catalog_json = "catalog.json"\n'
            '[agents]\n'
            'default_subagent_model = "fixture-other-model"\n'
            'experimental_realtime_webrtc_call_base_url = "https://example.invalid/backend-api/codex"\n'
            'experimental_realtime_ws_base_url = "https://example.invalid/v1"\n'
        )
        with self.assertRaisesRegex(
            SetupError,
            r'experimental_realtime_webrtc_call_base_url, experimental_realtime_ws_base_url',
        ):
            self.report()

    def test_recognized_agent_settings_and_role_tables_are_accepted(self):
        # The guard must not fire on a legitimate [agents] table: Codex accepts
        # these scalars there, and any other key is an agent name owning a table.
        self.config.write_text(
            self.config.read_text()
            + '[agents]\n'
            'enabled = true\n'
            'default_subagent_model = "fixture-other-model"\n'
            'default_subagent_reasoning_effort = "high"\n'
            'max_concurrent_threads_per_session = 6\n'
            'max_depth = 2\n'
            'job_max_runtime_seconds = 600\n'
            '[agents.fixture_other_role]\n'
            'description = "fixture role"\n'
        )
        self.assertEqual(self.report()['status'], 'static-ready')

    def test_documented_interrupt_and_legacy_thread_settings_are_accepted(self):
        original = self.config.read_text()
        for setting in ('interrupt_message = false', 'max_threads = 4'):
            with self.subTest(setting=setting):
                self.config.write_text(original + f'[agents]\n{setting}\n')
                self.assertEqual(self.report()['status'], 'static-ready')

    def test_agent_name_holding_a_scalar_is_rejected(self):
        # An agent name must own a role table; a bare scalar is the exact shape
        # Codex rejects with "expected struct AgentRoleToml".
        self.config.write_text(
            self.config.read_text() + '[agents]\nastra_flash_builder = "not-a-table"\n'
        )
        with self.assertRaisesRegex(SetupError, r'do not belong under \[agents\].*astra_flash_builder'):
            self.report()

    def test_standalone_profile_is_read_without_modifying_it(self):
        profile = self.codex / 'work.config.toml'
        profile.write_text('model = "fixture-profile-astra"\nmodel_reasoning_effort = "high"\n')
        result, _ = inspect(self.home, self.codex, 'work')
        self.assertEqual(result['root_model_observed'], 'fixture-profile-astra')
        self.assertEqual(result['worker_model'], ROUTE)
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_ambiguous_profiles_fail(self):
        self.config.write_text(self.config.read_text() + '\n[profiles.work]\nmodel = "fixture-old"\n')
        (self.codex / 'work.config.toml').write_text('model = "fixture-new"\n')
        with self.assertRaises(SetupError):
            inspect(self.home, self.codex, 'work')

    def test_global_worker_effort_is_ignored(self):
        self.config.write_text(self.config.read_text() + '\n[agents]\ndefault_subagent_reasoning_effort = "medium"\n')
        original = self.config.read_bytes()
        report = self.report()
        self.assertEqual(report['worker_effort'], 'high')
        self.assertEqual(self.config.read_bytes(), original)

    def test_existing_foreign_content_requires_explicit_replace(self):
        folder = self.home / '.agents' / 'skills' / SKILL
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('My existing custom content\n')
        with self.assertRaises(SetupError):
            self.changes()
        self.assertTrue(self.changes(replace=True))

    def test_symlink_destination_is_refused(self):
        external = self.home / 'external'
        external.mkdir()
        (self.home / '.agents').symlink_to(external, target_is_directory=True)
        with self.assertRaises(SetupError):
            self.changes()

    def test_legacy_same_name_installation_is_detected(self):
        (self.codex / 'skills' / SKILL).mkdir(parents=True)
        with self.assertRaises(SetupError):
            self.changes()

    def test_undo_restores_original_policy_and_removes_new_files(self):
        receipt = self.apply()
        result = self.cli('--undo', str(receipt), '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.policy.read_bytes(), self.original_policy)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        self.assertFalse((self.codex / 'agents' / f'{ROLE}.toml').exists())
        self.assertFalse((self.home / '.agents' / 'skills' / SKILL).exists())

    def test_undo_preserves_subsequent_user_edits_by_refusing(self):
        receipt = self.apply()
        self.policy.write_text(self.policy.read_text() + 'A later note.\n')
        changed = self.policy.read_bytes()
        result = self.cli('--undo', str(receipt), '--apply')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.policy.read_bytes(), changed)
        self.assertTrue((self.codex / 'agents' / f'{ROLE}.toml').exists())

    def test_install_rolls_back_completed_writes_on_error(self):
        report = self.report()
        changes = self.changes()
        target = changes[1]['path']
        real = install.atomic_write
        fired = False
        def failing_write(path, data, mode=0o600):
            nonlocal fired
            if path == target and not fired:
                fired = True
                raise OSError('Synthetic write failure')
            return real(path, data, mode)
        with patch.object(install, 'atomic_write', side_effect=failing_write):
            with self.assertRaises(OSError):
                install.apply_changes(changes, self.codex, report['input_hashes'])
        for change in changes:
            self.assertEqual(install.contents(change['path']), change['before'])
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_receipt_path_traversal_is_refused(self):
        receipt = self.apply()
        record = json.loads(receipt.read_text())
        record['files'][0]['path'] = str(self.home / '.agents' / 'skills' / SKILL / '..' / '..' / '..' / '.codex' / 'config.toml')
        receipt.write_text(json.dumps(record))
        result = self.cli('--undo', str(receipt), '--apply')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.config.read_bytes(), self.original_config)

    def test_installed_doctor_explicit_candidate_route_skips_role_drift_check(self):
        self.assertEqual(self.cli('--apply').returncode, 0)
        route = 'openrouter/deepseek-v4.1-flash'
        payload = json.loads(self.catalog.read_text())
        alternate = dict(payload['models'][0])
        alternate['slug'] = route
        payload['models'].append(alternate)
        self.catalog.write_text(json.dumps(payload))
        doctor = subprocess.run([sys.executable, str(self.home / '.agents' / 'skills' / SKILL / 'scripts' / 'doctor.py'),
                                 '--home', str(self.home), '--codex-home', str(self.codex),
                                 '--worker-route', route], capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)['worker_model'], route)

    def test_local_doctor_uses_only_models_get_without_exposing_capability(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps({'data': [{'id': 'openrouter/deepseek-v4.1-flash'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            route = 'openrouter/deepseek-v4.1-flash'
            self.set_catalog_route(route)
            self.config.write_text(self.config.read_text().replace(':4202/', f':{server.server_port}/'))
            result = subprocess.run([sys.executable, str(SCRIPTS / 'doctor.py'), '--home', str(self.home),
                                     '--codex-home', str(self.codex), '--worker-route', route,
                                     '--check-local-router'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(seen, ['/_codex-router/TEST_PRIVATE_CAPABILITY/v1/models'])
            self.assertNotIn('TEST_PRIVATE_CAPABILITY', result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['worker_model'], route)
            self.assertFalse(report['runtime_verified'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_config_changed_after_preflight_is_detected(self):
        report = self.report()
        changes = self.changes()
        self.config.write_text(self.config.read_text() + '# Changed by another process\n')
        with self.assertRaises(SetupError):
            install.apply_changes(changes, self.codex, report['input_hashes'])


class NativeSetupTests(unittest.TestCase):
    setUp = SetupFixture.setUp
    tearDown = SetupFixture.tearDown
    cli = SetupFixture.cli
    set_catalog_route = SetupFixture.set_catalog_route
    MODEL = "fixture-openai-worker"

    def setUp(self):
        SetupFixture.setUp(self)
        self.config.write_text(
            self.config.read_text().replace(
                'openai_base_url = "http://127.0.0.1:4202/_codex-router/TEST_PRIVATE_CAPABILITY/v1"\n', ''
            )
        )
        self.original_config = self.config.read_bytes()
        self.set_catalog_route(self.MODEL)

    def native(self, *args):
        return self.cli('--worker-model', self.MODEL, *args)

    def test_native_install_generates_role_binding_and_preserves_config(self):
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        preview = self.native()
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        result = self.native('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        role = tomllib.loads((self.codex / 'agents' / f'{ROLE}.toml').read_text())
        binding = json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())
        self.assertEqual(role['model'], self.MODEL)
        self.assertEqual(role['model_provider'], 'openai')
        self.assertEqual(role['model_reasoning_effort'], 'high')
        self.assertFalse(role['agents']['enabled'])
        self.assertEqual(binding['backend'], BACKEND_NATIVE)
        self.assertEqual(binding['worker_model'], self.MODEL)
        self.assertEqual(binding['model_catalog_source'], str(self.catalog))
        self.assertNotIn('DeepSeek', role['developer_instructions'])
        self.assertNotIn('Router', role['developer_instructions'])
        doctor = subprocess.run([sys.executable, str(self.home / '.agents' / 'skills' / SKILL / 'scripts' / 'doctor.py'),
                                 '--home', str(self.home), '--codex-home', str(self.codex)],
                                capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(json.loads(doctor.stdout)['backend'], BACKEND_NATIVE)

    def test_native_selection_and_effort_fail_closed(self):
        cases = [
            (('--worker-route', ROUTE), 'conflict'),
            (('--worker-effort', 'unsupported'), 'not supported'),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                result = self.native(*extra)
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
        self.assertEqual(self.cli('--worker-effort', 'high').returncode, 2)
        self.assertEqual(self.cli('--worker-model', 'missing').returncode, 2)
        self.assertFalse((self.home / '.agents').exists())

    def test_native_preserves_auth_other_agents_and_root_settings(self):
        auth = self.codex / 'auth.json'
        auth.write_text('{"secret": "TEST_NATIVE_AUTH_SECRET"}')
        other = self.codex / 'agents' / 'other.toml'
        other.parent.mkdir()
        other.write_text('model = "other-model"\n')
        self.config.write_text(self.config.read_text() + '\n[agents]\ndefault_subagent_model = "other-model"\n')
        original_config = self.config.read_bytes()
        original_auth = auth.read_bytes()
        original_other = other.read_bytes()
        result = self.native('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), original_config)
        self.assertEqual(auth.read_bytes(), original_auth)
        self.assertEqual(other.read_bytes(), original_other)
        self.assertNotIn('TEST_NATIVE_AUTH_SECRET', result.stdout + result.stderr)

    def test_native_duplicate_and_missing_default_effort(self):
        payload = json.loads(self.catalog.read_text())
        payload['models'].append(payload['models'][0])
        self.catalog.write_text(json.dumps(payload))
        self.assertIn('missing or duplicated', self.native().stderr)
        payload['models'].pop()
        payload['models'][0].pop('default_reasoning_level')
        self.catalog.write_text(json.dumps(payload))
        self.assertIn('Cannot verify a default', self.native().stderr)
        self.assertEqual(self.native('--worker-effort', 'max').returncode, 0)

    def test_recorded_export_survives_later_cache_creation(self):
        self.config.unlink()
        exported = self.home / 'exported.json'
        self.catalog.rename(exported)
        self.assertEqual(self.cli('--worker-model', self.MODEL, '--model-catalog', str(exported), '--apply').returncode, 0)
        (self.codex / 'models_cache.json').write_text('{"models": []}')
        result = self.cli('--replace', '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        binding = json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())
        self.assertEqual(binding['model_catalog_source'], str(exported))

    def test_provider_qualified_and_non_openai_entries_are_rejected(self):
        self.set_catalog_route('openrouter/other-model')
        self.assertIn('provider-qualified', self.cli('--worker-model', 'openrouter/other-model').stderr)
        payload = json.loads(self.catalog.read_text())
        payload['models'][0]['slug'] = self.MODEL
        payload['models'][0]['provider'] = 'OpenRouter'
        self.catalog.write_text(json.dumps(payload))
        self.assertIn('non-OpenAI provider', self.native().stderr)

    def test_native_endpoint_config_guards_and_no_policy(self):
        original = self.config.read_text()
        for extra in ('openai_base_url = "https://example.invalid/v1"\n',
                      'chatgpt_base_url = "https://example.invalid/api/"\n',
                      '[model_providers.openai]\nbase_url = "https://example.invalid/v1"\n'):
            with self.subTest(extra=extra):
                self.config.write_text(
                    original + extra if extra.startswith('[model_providers.')
                    else original.replace('[model_providers.unused]', extra + '[model_providers.unused]')
                )
                self.assertEqual(self.native().returncode, 2)
        self.config.write_text(original)
        before = self.policy.read_bytes()
        self.assertEqual(self.native('--no-policy', '--apply').returncode, 0)
        self.assertEqual(self.policy.read_bytes(), before)

    def test_native_invalid_or_missing_model_argument(self):
        self.assertEqual(self.cli('--worker-model').returncode, 2)
        self.assertIn('nonempty exact model', self.cli('--worker-model', ' ').stderr)

    def test_relative_export_resolves_from_invocation_directory(self):
        self.config.unlink()
        exported = self.home / 'exported.json'
        self.catalog.rename(exported)
        result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--home', str(self.home),
                                 '--codex-home', str(self.codex), '--worker-model', self.MODEL,
                                 '--model-catalog', 'exported.json'], cwd=self.home,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(exported), result.stdout)

    def test_custom_codex_home_and_no_config(self):
        alternate = self.home / 'alternate-codex'
        alternate.mkdir()
        self.config.unlink()
        exported = self.home / 'exported.json'
        self.catalog.rename(exported)
        result = subprocess.run([sys.executable, str(ROOT / 'install.py'), '--home', str(self.home),
                                 '--codex-home', str(alternate), '--worker-model', self.MODEL,
                                 '--model-catalog', str(exported), '--apply'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((alternate / 'agents' / f'{ROLE}.toml').exists())
        self.assertFalse((alternate / 'config.toml').exists())

    def test_native_inspect_and_install_do_not_read_auth_or_call_network(self):
        import socket
        import urllib.request
        auth = self.codex / 'auth.json'
        auth.write_text('{"secret": "TEST_NATIVE_AUTH_SECRET"}')
        original_open = Path.open
        def watched_open(path, *args, **kwargs):
            if path == auth:
                raise AssertionError('credential file read')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', watched_open), \
             patch.object(subprocess, 'run', side_effect=AssertionError('subprocess')), \
             patch.object(socket, 'create_connection', side_effect=AssertionError('network')), \
             patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network')):
            report, _ = inspect(self.home, self.codex, selection={
                'backend': BACKEND_NATIVE, 'model': self.MODEL})
            changes = install.plan_changes(self.home, self.codex, report, True, False)
            install.apply_changes(changes, self.codex, report['input_hashes'])

    def test_native_capability_and_provider_checks(self):
        original = self.catalog.read_text()
        for metadata, expected in [
            ({'multi_agent_version': 'v1'}, 'incompatible'),
            ({'multi_agent_version': None}, 'Cannot verify'),
            ({'supported_reasoning_levels': []}, 'Cannot verify'),
        ]:
            with self.subTest(metadata=metadata):
                payload = json.loads(original)
                payload['models'][0].update(metadata)
                self.catalog.write_text(json.dumps(payload))
                result = self.native()
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
        self.catalog.write_text(original)
        self.config.write_text(self.config.read_text().replace('model = "fixture-astra-root"',
                                                          'model_provider = "custom"\nmodel = "fixture-astra-root"'))
        self.assertIn('parent provider', self.native().stderr)

    def test_native_upgrade_switch_and_guarded_undo(self):
        first = self.native('--worker-effort', 'max', '--apply')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.cli().returncode, 0)  # optionless upgrade preserves native binding
        updated = self.cli('--replace', '--apply')
        self.assertEqual(updated.returncode, 0, updated.stderr)
        binding = json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())
        self.assertEqual(binding['backend'], BACKEND_NATIVE)
        self.assertEqual(binding['worker_effort'], 'max')
        self.assertEqual(self.cli('--worker-route', ROUTE, '--replace').returncode, 2)
        self.set_catalog_route(ROUTE)
        self.config.write_text(self.config.read_text().replace(
            'model_catalog_json = "catalog.json"',
            'openai_base_url = "http://127.0.0.1:4202/v1"\nmodel_catalog_json = "catalog.json"'))
        switch = self.cli('--worker-route', ROUTE, '--replace', '--apply')
        self.assertEqual(switch.returncode, 0, switch.stderr)
        self.assertEqual(json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())['backend'], 'router')
        receipt = Path(switch.stdout.split('Undo receipt: ')[1].splitlines()[0])
        self.assertEqual(self.cli('--undo', str(receipt), '--apply').returncode, 0)
        self.assertEqual(json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())['backend'], BACKEND_NATIVE)

    def test_native_explicit_catalog_without_config_or_router(self):
        self.config.unlink()
        self.catalog.rename(self.home / 'exported.json')
        exported = self.home / 'exported.json'
        result = self.cli('--worker-model', self.MODEL, '--model-catalog', str(exported), '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.config.exists())
        self.assertEqual(json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())['model_catalog_source'],
                         str(exported))

    def test_native_cache_and_override_checks(self):
        self.config.write_text(self.config.read_text().replace('model_catalog_json = "catalog.json"\n', ''))
        cache = self.codex / 'models_cache.json'
        cache.write_bytes(self.catalog.read_bytes())
        result = self.native()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('capability snapshot', result.stdout)
        with patch.dict('os.environ', {'OPENAI_BASE_URL': 'http://example.invalid/v1'}):
            result = self.native()
        self.assertEqual(result.returncode, 2)
        self.assertIn('endpoint override', result.stderr)
        self.assertNotIn('example.invalid', result.stderr)

    def test_native_profile_and_inline_shadowing(self):
        profile = self.codex / 'work.config.toml'
        profile.write_text('model = "fixture-profile-root"\n')
        result = self.native('--profile', 'work', '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        binding = self.home / '.agents' / 'skills' / SKILL / 'routing.json'
        self.assertEqual(json.loads(binding.read_text())['profile_inspected'], 'work')
        self.assertEqual(self.cli('--replace').returncode, 0)
        self.config.write_text(self.config.read_text() + f'\n[agents.{ROLE}]\ndescription = "shadow"\n')
        self.assertIn('compete', self.cli('--replace').stderr)

    def test_native_doctor_router_check_rejected(self):
        self.assertEqual(self.native('--apply').returncode, 0)
        doctor = subprocess.run([sys.executable, str(self.home / '.agents' / 'skills' / SKILL / 'scripts' / 'doctor.py'),
                                 '--home', str(self.home), '--codex-home', str(self.codex),
                                 '--check-local-router'], capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 2)
        self.assertIn('only to Router', doctor.stderr)

    def test_config_appearing_after_native_preflight_blocks_apply(self):
        self.config.unlink()
        report, _ = inspect(self.home, self.codex, selection={
            'backend': BACKEND_NATIVE, 'model': self.MODEL, 'catalog': str(self.catalog)})
        changes = install.plan_changes(self.home, self.codex, report, True, False)
        self.config.write_text('model_provider = "custom"\n')
        with self.assertRaisesRegex(SetupError, 'changed during inspection'):
            install.apply_changes(changes, self.codex, report['input_hashes'])

    def test_native_switch_with_stale_managed_policy_requires_policy_update(self):
        self.assertEqual(self.native('--apply').returncode, 0)
        old = self.policy.read_bytes()
        self.policy.write_bytes(old.replace(b'uses the selected installed backend', b'uses an old Flash-only route'))
        result = self.cli('--replace', '--no-policy')
        self.assertEqual(result.returncode, 2)
        self.assertIn('outdated managed workflow', result.stderr)

    def test_legacy_binding_resolves_as_router(self):
        binding = self.home / 'legacy-routing.json'
        binding.write_text(json.dumps({'worker_model': ROUTE, 'worker_provider': 'DeepSeek API',
                                       'worker_effort': 'high', 'profile_inspected': 'work'}))
        selection = resolve_worker_selection(binding=binding)
        self.assertEqual(selection['backend'], 'router')
        self.assertEqual(selection['model'], ROUTE)
        self.assertEqual(selection['profile'], 'work')

    def test_policy_target_move_removes_old_managed_block(self):
        self.assertEqual(self.native('--apply').returncode, 0)
        override = self.codex / 'AGENTS.override.md'
        override.write_text('New active override.\n')
        result = self.cli('--replace', '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(install.BEGIN, self.policy.read_bytes())
        self.assertIn(b'Preserve my unrelated notes.', self.policy.read_bytes())
        self.assertIn(install.BEGIN, override.read_bytes())
        self.assertTrue(override.read_text().startswith('New active override.'))

    def test_legacy_deepseek_to_native_switch_and_guarded_undo(self):
        self.set_catalog_route(ROUTE)
        self.config.write_text(self.config.read_text().replace(
            'model_catalog_json = "catalog.json"',
            'openai_base_url = "http://127.0.0.1:4202/v1"\nmodel_catalog_json = "catalog.json"'))
        self.assertEqual(self.cli('--apply').returncode, 0)
        self.set_catalog_route(self.MODEL)
        self.config.write_text(self.config.read_text().replace(
            'openai_base_url = "http://127.0.0.1:4202/v1"\n', ''))
        result = self.native('--replace', '--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = Path(result.stdout.split('Undo receipt: ')[1].splitlines()[0])
        role = self.codex / 'agents' / f'{ROLE}.toml'
        original = role.read_bytes()
        role.write_bytes(original + b'\n# Later edit\n')
        self.assertEqual(self.cli('--undo', str(receipt), '--apply').returncode, 2)
        role.write_bytes(original)
        self.assertEqual(self.cli('--undo', str(receipt), '--apply').returncode, 0)
        binding = json.loads((self.home / '.agents' / 'skills' / SKILL / 'routing.json').read_text())
        self.assertEqual(binding['backend'], 'router')
        self.assertEqual(binding['worker_model'], ROUTE)

    def test_native_shared_root_guard_and_legacy_feature_warning(self):
        self.config.write_text(self.config.read_text().replace('fixture-astra-root', ROUTE))
        self.assertIn('root model is Flash', self.native().stderr)
        self.config.write_text(self.config.read_text().replace(ROUTE, 'fixture-astra-root')
                               + '\n[features]\nmulti_agent = false\n')
        self.assertIn('features.multi_agent=false', self.native().stdout)

    def test_native_doctor_rejects_role_name_drift(self):
        self.assertEqual(self.native('--apply').returncode, 0)
        role = self.codex / 'agents' / f'{ROLE}.toml'
        role.write_text(role.read_text().replace(f'name = "{ROLE}"', 'name = "different"'))
        doctor = subprocess.run([sys.executable, str(self.home / '.agents' / 'skills' / SKILL / 'scripts' / 'doctor.py'),
                                 '--home', str(self.home), '--codex-home', str(self.codex)],
                                capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 2)
        self.assertIn('role name differs', doctor.stderr)

    def test_read_only_symlinked_config_and_catalog_remain_accepted(self):
        real_config = self.codex / 'real-config.toml'
        real_catalog = self.codex / 'real-catalog.json'
        self.config.rename(real_config)
        self.config.symlink_to(real_config)
        self.catalog.rename(real_catalog)
        self.catalog.symlink_to(real_catalog)
        result = self.native('--apply')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.config.is_symlink())
        self.assertTrue(self.catalog.is_symlink())

    def test_native_doctor_rejects_role_drift(self):
        self.assertEqual(self.native('--apply').returncode, 0)
        role = self.codex / 'agents' / f'{ROLE}.toml'
        role.write_text(role.read_text().replace(f'model = "{self.MODEL}"', 'model = "other"'))
        doctor = subprocess.run([sys.executable, str(self.home / '.agents' / 'skills' / SKILL / 'scripts' / 'doctor.py'),
                                 '--home', str(self.home), '--codex-home', str(self.codex)],
                                capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 2)
        self.assertIn('differs from routing.json', doctor.stderr)


class PolicyTests(unittest.TestCase):
    def test_worker_route_resolution_defaults_and_reuses_valid_binding(self):
        self.assertEqual(resolve_worker_route(), ROUTE)
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory).resolve() / 'routing.json'
            route = 'openrouter/deepseek-v4.1-flash'
            binding.write_text(json.dumps({'worker_model': route}))
            self.assertEqual(resolve_worker_route(binding=binding), route)
            self.assertEqual(resolve_worker_route(ROUTE, binding), ROUTE)

    def test_worker_route_resolution_rejects_malformed_or_unreviewed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory).resolve() / 'routing.json'
            binding.write_text('{broken')
            with self.assertRaises(SetupError):
                resolve_worker_route(binding=binding)
            binding.write_text(json.dumps({'worker_model': 'custom/deepseek-v4.1-flash'}))
            with self.assertRaisesRegex(SetupError, 'Unsupported worker route'):
                resolve_worker_route(binding=binding)

    def test_worker_route_resolution_refuses_symlinked_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / 'target.json'
            target.write_text(json.dumps({'worker_model': ROUTE}))
            binding = root / 'routing.json'
            binding.symlink_to(target)
            with self.assertRaisesRegex(SetupError, 'symlinked routing binding'):
                resolve_worker_route(binding=binding)

    def test_crlf_and_unrelated_text_are_preserved(self):
        block = install.BEGIN + b'\nnew\n' + install.END + b'\n'
        old = b'prefix\r\n' + install.BEGIN + b'\r\nold\r\n' + install.END + b'\r\nsuffix\r\n'
        updated = install.managed_policy(old, block)
        self.assertTrue(updated.startswith(b'prefix\r\n'))
        self.assertTrue(updated.endswith(b'suffix\r\n'))
        self.assertEqual(updated.count(install.BEGIN), 1)
        self.assertIn(b'new\r\n', updated)

    def test_malformed_markers_are_refused(self):
        with self.assertRaises(SetupError):
            install.managed_policy(install.BEGIN, b'new')

    def test_redirect_refused_before_forwarding(self):
        from doctor import NoRedirect
        with self.assertRaises(SetupError):
            NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.invalid/secret')

    def test_catalog_shapes(self):
        for payload in ([{'id': ROUTE}], {'data': [{'id': ROUTE}]}, {'models': [{'slug': ROUTE}]}):
            self.assertEqual(len(model_entries(payload)), 1)
        with self.assertRaises(SetupError):
            model_entries({'not_models': []})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'examples' / 'invoice-filter'
        self.plan = json.loads((self.root / 'plan.json').read_text())

    def test_example_is_structurally_valid(self):
        self.assertEqual(validate(self.plan, self.root)['status'], 'structure-valid')

    def test_placeholder_is_rejected(self):
        self.plan['tasks'][0]['acceptance'] = ['TODO']
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_missing_brief_is_rejected(self):
        self.plan['tasks'][0]['brief'] = 'missing.md'
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_unbounded_file_scope_is_rejected(self):
        for path in ['.', '../outside', '/absolute', 'src/**', '.git/config']:
            self.plan['tasks'][0]['allowed_paths'] = [path]
            with self.assertRaises(PlanError):
                validate(self.plan, self.root)

    def test_sensitive_task_cannot_be_assigned_to_flash(self):
        self.plan['tasks'][0]['risk'] = 'sensitive'
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def add_task(self):
        task = copy.deepcopy(self.plan['tasks'][0])
        task['id'] = 'T2'
        task['allowed_paths'] = ['src/other.py', 'tests/test_other.py']
        self.plan['tasks'].append(task)
        return task

    def test_dependency_cycle_is_rejected(self):
        second = self.add_task()
        second['depends_on'] = ['T1']
        self.plan['tasks'][0]['depends_on'] = ['T2']
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_duplicate_id_is_rejected(self):
        self.add_task()['id'] = 'T1'
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_parallel_independent_scopes_are_accepted(self):
        self.add_task()
        self.plan['max_flash_workers'] = 2
        for task in self.plan['tasks']:
            task['parallel_group'] = 'G1'
        validate(self.plan, self.root)

    def test_parallel_overlap_is_rejected(self):
        self.add_task()['allowed_paths'] = ['src/invoices/']
        self.plan['max_flash_workers'] = 2
        for task in self.plan['tasks']:
            task['parallel_group'] = 'G1'
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_parallel_dependency_is_rejected(self):
        self.add_task()['depends_on'] = ['T1']
        self.plan['max_flash_workers'] = 2
        for task in self.plan['tasks']:
            task['parallel_group'] = 'G1'
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_cross_phase_dependency_must_be_declared(self):
        second = self.add_task()
        self.plan['phases'].append({'id': 'P2', 'goal': 'Second milestone', 'depends_on': ['P1'],
                                  'integration_checks': [{'command': 'python3 -m unittest', 'expected': 'Tests pass'}]})
        second['phase'] = 'P2'
        self.plan['tasks'][0]['depends_on'] = ['T2']
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)

    def test_excessive_parallelism_is_rejected(self):
        self.plan['max_flash_workers'] = 99
        with self.assertRaises(PlanError):
            validate(self.plan, self.root)


if __name__ == '__main__':
    unittest.main()
