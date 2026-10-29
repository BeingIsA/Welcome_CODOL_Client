"""主电影重载生命周期回归；模拟加载不替代原生返回大厅验收。"""
from pathlib import Path
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import Mock
import zlib

from codol_rebuild.adapters.game.v1649370.session import ui_movie
from codol_rebuild.adapters.game.v1649370.session.session_bridge import GameSessionBridge


class MovieReloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (Path(__file__).resolve().parents[2] / 'research/client/ui-investigation/codo.gfx').read_bytes()
        cls.patched = ui_movie.patch_movie(cls.original)
        zone = (Path(__file__).resolve().parents[2] / 'research/client/ui-investigation/ui_mp.zone').read_bytes()
        start = zone.index(ui_movie.RESOURCE_NAME) + len(ui_movie.RESOURCE_NAME)
        cls.original_compressed = zone[start:start + ui_movie.COMPRESSED_SIZE]

    def memory(self, movie):
        compressed = self.original_compressed if movie == self.original else zlib.compress(movie, 9)
        blocks = {0x1000: struct.pack('<4I', 0x2000, len(compressed), len(movie), 0x3000),
                  0x2000: ui_movie.RESOURCE_NAME, 0x3000: compressed}
        def read(address, length):
            for base, data in blocks.items():
                offset = address - base
                if 0 <= offset and offset + length <= len(data):
                    return data[offset:offset + length]
            raise OSError('fixture boundary')
        return read

    def test_new_and_already_patched_resource_have_same_complete_digest(self):
        first = ui_movie.find_resource_patch(self.memory(self.original), [(0x1000, 16)])
        repeated = ui_movie.find_resource_patch(self.memory(self.patched), [(0x1000, 16)])
        self.assertFalse(first['already_applied'])
        self.assertTrue(repeated['already_applied'])
        self.assertEqual(first['patched_sha256'], repeated['patched_sha256'])
        self.assertEqual(repeated['data_before'], repeated['data_after'])
        # 返回大厅的新资源实例即使复用了旧地址，也必须重新校验并安装。
        reloaded = ui_movie.find_resource_patch(self.memory(self.original), [(0x1000, 16)])
        self.assertFalse(reloaded['already_applied'])

    def test_unknown_bytes_outside_patch_windows_are_rejected(self):
        changed = bytearray(self.patched)
        changed[-1] ^= 1
        with self.assertRaises(ValueError):
            ui_movie.build_resource_patch(self.memory(bytes(changed)), 0x1000)

    def test_load_return_cycle_filters_ingame_and_rearms_lobby(self):
        bridge = object.__new__(GameSessionBridge)
        bridge.process = Mock()
        bridge.process.read.side_effect = lambda address, length: bytes(length)
        bridge.debug = Mock()
        bridge.args = SimpleNamespace(capture_schema=False)
        bridge.install_navigation_resource = Mock(return_value={'installed': True})
        context = bytearray(716)
        for name in ('scaleform/default/codo.gfx', 'scaleform/default/codoingame.gfx',
                     'scaleform/default/codo.gfx', 'scaleform/default/codo.gfx'):
            bridge.process.cstring.return_value = name
            bridge.breakpoint(None, ui_movie.LOAD_SITE, ui_movie.LOAD_BYTES[:1],
                              'Local navigation resource', {}, None, context)
            bridge.debug.arm.assert_called_with(ui_movie.LOAD_RETURN_SITE, 'Local navigation loaded')
            bridge.breakpoint(None, ui_movie.LOAD_RETURN_SITE, ui_movie.LOAD_RETURN_BYTES[:1],
                              'Local navigation loaded', {}, None, context)
            bridge.debug.arm.assert_called_with(ui_movie.LOAD_SITE, 'Local navigation resource')
        self.assertEqual(bridge.install_navigation_resource.call_count, 3)

    def test_verified_patched_resource_is_not_written_again(self):
        bridge = object.__new__(GameSessionBridge)
        bridge.process = Mock()
        bridge.process.read.side_effect = self.memory(self.patched)
        bridge.process.regions.return_value = [(0x1000, 16)]
        result = bridge.install_navigation_resource()
        self.assertTrue(result['already_applied'])
        bridge.process.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
