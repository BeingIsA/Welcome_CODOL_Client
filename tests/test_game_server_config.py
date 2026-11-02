"""精简网络配置、动态地址选择及离线启动回归。"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codol_rebuild.bootstrap.config import read_server_config, parse_args
from codol_rebuild.bootstrap.launch import select_client_address

class GameServerConfigurationTests(unittest.TestCase):
    def read_configuration(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.ini'
            path.write_text(content, encoding='utf-8')
            return read_server_config(path)

    def test_local_and_remote_share_configuration(self):
        for address in ('127.0.0.1', '172.22.26.229'):
            self.assertEqual(self.read_configuration(f'[server]\nip={address}\nport=8080\n[client]\nport=30002'),
                             (f'http://{address}:8080', 30002))

    def test_default_ports(self):
        self.assertEqual(self.read_configuration('[server]\nip=127.0.0.1'), ('http://127.0.0.1:8080', 30001))

    def test_invalid_configuration(self):
        for address, server_port, client_port in [('0.0.0.0', 8080, 30001), ('::1', 8080, 30001),
                ('224.0.0.1', 8080, 30001), ('127.0.0.1', 0, 30001), ('127.0.0.1', 8080, 65536)]:
            with self.subTest(address=address, server_port=server_port, client_port=client_port), self.assertRaises(ValueError):
                self.read_configuration(f'[server]\nip={address}\nport={server_port}\n[client]\nport={client_port}')

    def test_server_override_is_single_authority(self):
        parsed = parse_args(['--name', 'test', '--local-profile', '--server-url', 'http://127.0.0.1:9090', '--client-port', '30002'])
        self.assertEqual(parsed.server_address, '127.0.0.1')
        self.assertEqual(parsed.client_port, 30002)

    def test_offline_has_no_server_address(self):
        self.assertIsNone(parse_args(['--name', 'test']).server_address)

    def test_removed_arguments_are_not_supported(self):
        for option in ('--game-server-ip', '--game-server-port', '--game-server-port-end',
                       '--game-network-mode', '--game-bind-ip', '--game-bind-port'):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(['--name', 'test', option, '1'])

    def test_route_selected_each_time_without_sending(self):
        route_socket = MagicMock()
        route_socket.__enter__.return_value = route_socket
        route_socket.getsockname.side_effect = [('127.0.0.1', 41000), ('192.168.1.35', 41001), ('192.168.1.36', 41002)]
        with patch('codol_rebuild.bootstrap.launch.socket.socket', return_value=route_socket):
            self.assertEqual(select_client_address('http://127.0.0.1:8080'), '127.0.0.1')
            self.assertEqual(select_client_address('http://172.22.26.229:8080'), '192.168.1.35')
            self.assertEqual(select_client_address('http://172.22.26.229:8080'), '192.168.1.36')
        route_socket.send.assert_not_called()
        route_socket.sendto.assert_not_called()
        route_socket.bind.assert_not_called()
        self.assertEqual(route_socket.__exit__.call_count, 3)

    def test_route_failure_is_not_replaced_by_loopback(self):
        with patch('codol_rebuild.bootstrap.launch.socket.socket', side_effect=OSError('no route')):
            with self.assertRaises(OSError):
                select_client_address('http://172.22.26.229:8080')
