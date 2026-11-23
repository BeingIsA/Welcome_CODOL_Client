"""验证编译入口和源码入口的启动参数，不启动游戏或依赖本机原包。"""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class HostLaunchCliTests(unittest.TestCase):
    def setUp(self):
        launcher_path = Path(__file__).resolve().parents[1] / 'tools/launch_client.py'
        module_spec = importlib.util.spec_from_file_location('launch_client_for_test', launcher_path)
        self.launcher = importlib.util.module_from_spec(module_spec)
        with patch.object(sys, 'path', sys.path.copy()):
            module_spec.loader.exec_module(self.launcher)

    def launch_options(self, arguments):
        with patch.object(self.launcher, 'build_plan', return_value={}) as build_plan, patch('builtins.print'):
            self.assertEqual(self.launcher.main(['--dry-run', *arguments]), 0)
        return build_plan.call_args.args[0]

    def test_compiled_defaults_match_source_cmd_and_accept_explicit_overrides(self):
        with patch('codol_rebuild.bootstrap.config.read_server_config',
                   return_value=('http://127.0.0.1:8080', 30001)) as read_config:
            source = self.launch_options([])
            self.assertFalse(source.local_profile)
            self.assertIsNone(source.server_config)
            read_config.assert_not_called()
            default_config = self.launcher.ROOT / 'config/server.ini'
            cmd_options = self.launch_options(['--local-profile', '--server-config', str(default_config)])
            with patch.object(self.launcher, '__compiled__', object(), create=True):
                compiled = self.launch_options([])
                self.assertEqual(vars(compiled), vars(cmd_options))
                read_config.assert_called_with(default_config)
                specific_config = Path('custom.ini')
                custom = self.launch_options(['--server-config', str(specific_config),
                                               '--server-url', 'http://127.0.0.1:18080', '--seconds', '15'])
            read_config.assert_called_with(specific_config)
            self.assertEqual(custom.server_url, 'http://127.0.0.1:18080')
            self.assertEqual(custom.seconds, 15)

    def test_windowless_start_reports_preflight_failure_in_native_dialog(self):
        error = '缺少游戏资源'
        with (patch.object(self.launcher, '__compiled__', object(), create=True),
              patch('codol_rebuild.bootstrap.config.read_server_config', return_value=('http://localhost', 30001)),
              patch.object(self.launcher, 'build_plan', side_effect=ValueError(error)),
              patch.object(sys, 'stderr', None), patch.object(sys, 'stdout', None),
              patch('ctypes.windll.user32.MessageBoxW') as message_box):
            self.assertEqual(self.launcher.main([]), 1)
            message_box.assert_called_once_with(None, error, 'CODOL 启动失败', 0x10)

    def test_redirected_output_receives_error_without_dialog(self):
        with (patch.object(self.launcher, '__compiled__', object(), create=True),
              patch.object(sys, 'stderr', None), patch.object(sys, 'stdout', io.StringIO()) as output,
              patch('ctypes.windll.user32.MessageBoxW') as message_box):
            self.launcher.report_start_error('资源缺失')
            self.assertEqual(json.loads(output.getvalue())['error'], '资源缺失')
            message_box.assert_not_called()
