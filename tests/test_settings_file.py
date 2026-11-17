import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('settings_relay', ROOT / 'spot-collector.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class SettingsFileTests(unittest.TestCase):
    def build(self, folder, **extra):
        args = relay._create_parser(False).parse_args([])
        args.config = str(Path(folder) / 'config.json')
        server = dict(address='localhost:8300', role='gocluster', direction='in',
                      callsign='TK0C', settings_file='GO.settings', ready_text='Hello TK0C, this is')
        server.update(extra)
        args.servers_config_list = [server]
        return relay.build_server_definitions(args)[0]

    def test_relative_to_config_comments_and_command_order(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'GO.settings').write_text('# settings\r\n\r\nPASS SELF\r\nSET DIAG OFF\r\n')
            self.assertEqual(self.build(folder)['startup_commands'], ['PASS SELF', 'SET DIAG OFF'])

    def test_invalid_files_fail_before_connection(self):
        for payload in (None, b'x' * 16385, b'\xff', b'PASS SELF\n' * 17):
            with self.subTest(payload=repr(payload)[:40]), tempfile.TemporaryDirectory() as folder:
                if payload is not None:
                    Path(folder, 'GO.settings').write_bytes(payload)
                with self.assertRaises(ValueError):
                    self.build(folder)

    def test_exclusive_inline_and_explicit_readiness(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'GO.settings').write_text('PASS SELF\n')
            for extra in ({'startup_commands': ['PASS SELF']}, {'ready_text': ''}):
                with self.assertRaises(ValueError):
                    self.build(folder, **extra)

    def test_shipped_go_settings_and_endpoint(self):
        args = relay._create_parser(False).parse_args([])
        args.config = str(ROOT / 'spot-collector.json')
        args.servers_config_list = json.loads(Path(args.config).read_text())['servers']
        server = next(s for s in relay.build_server_definitions(args) if s['role'] == 'gocluster')
        self.assertEqual(server['address'], 'cluster.n2wq.com:8300')
        self.assertEqual(server['callsign'], 'TK0C')
        self.assertTrue(server['enabled'])
        self.assertEqual(server.get('startup_commands', []), [])
        self.assertNotIn('settings_file', server)
        self.assertNotIn('ready_text', server)
        self.assertTrue((ROOT / 'GO.settings').is_file())
