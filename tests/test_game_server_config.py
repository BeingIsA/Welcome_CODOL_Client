"""游戏服务器配置分节、默认关闭和回环约束回归。"""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codol_rebuild.bootstrap.config import read_server_config, parse_args

class GameServerConfigurationTests(unittest.TestCase):
    def read_configuration(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'server.ini'
            path.write_text(content, encoding='utf-8')
            return read_server_config(path)

    def test_separate_endpoints(self):
        self.assertEqual(self.read_configuration('[server]\nip=127.0.0.1\nport=8080\n[game_server]\nenabled=true\nip=127.0.0.2\nport=27760'),
                         ('http://127.0.0.1:8080', ('127.0.0.2', 27760), None))

    def test_disabled_or_absent(self):
        for suffix in ('', '\n[game_server]\nenabled=false'):
            self.assertEqual(self.read_configuration('[server]\nip=127.0.0.1'+suffix)[1], None)

    def test_invalid_endpoint(self):
        for address, port in [('192.168.1.1', '27760'), ('::1', '27760'), ('localhost', '27760'), ('127.0.0.1', '0'), ('127.0.0.1', '65536')]:
            with self.subTest(address=address, port=port), self.assertRaises(ValueError):
                self.read_configuration(f'[server]\nip=127.0.0.1\n[game_server]\nenabled=true\nip={address}\nport={port}')

    def test_host_argument_binding(self):
        parsed = parse_args(['--name', 'test', '--game-server-ip', '127.0.0.2', '--game-server-port', '27760'])
        self.assertEqual((parsed.game_server_ip, parsed.game_server_port), ('127.0.0.2', 27760))

    def test_local_network_requires_enabled_endpoint(self):
        section = '\n[game_network]\nmode=local\nbind_ip=127.0.0.1\nbind_port=30001'
        for endpoint in ('', '\n[game_server]\nenabled=false'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.read_configuration('[server]\nip=127.0.0.1'+endpoint+section)
        configuration = self.read_configuration('[server]\nip=127.0.0.1\n[game_server]\nenabled=true\nip=127.0.0.2\nport=27760'+section)
        self.assertEqual(configuration[2], ('local', '127.0.0.1', 30001))

    def test_network_binding_and_mode_are_checked(self):
        prefix = '[server]\nip=127.0.0.1\n[game_server]\nenabled=true\nip=127.0.0.2\nport=27760\n[game_network]\n'
        for mode, address, port in [('remote', '127.0.0.1', 30001), ('local', '192.168.1.1', 30001), ('local', '127.0.0.1', 0)]:
            with self.subTest(mode=mode, address=address, port=port), self.assertRaises(ValueError):
                self.read_configuration(prefix+f'mode={mode}\nbind_ip={address}\nbind_port={port}')
