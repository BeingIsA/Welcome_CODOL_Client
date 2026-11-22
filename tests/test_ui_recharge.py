"""确认运行时主电影完整保留原版账号栏的充值按钮与网页事件。"""
from pathlib import Path
import struct
import unittest

from codol_rebuild.adapters.game.v1649370.session import ui_movie


def account_component(movie):
    """提取 PlayerAccountInfo 的 DoABC 标签，覆盖方法体、常量池与绑定信息。"""
    rectangle_bits = movie[8] >> 3
    tag_offset = 8 + (5 + 4 * rectangle_bits + 7) // 8 + 4
    while tag_offset < len(movie):
        header = struct.unpack_from("<H", movie, tag_offset)[0]
        tag_offset += 2
        tag_length = header & 63
        if tag_length == 63:
            tag_length = struct.unpack_from("<I", movie, tag_offset)[0]
            tag_offset += 4
        tag_data = movie[tag_offset:tag_offset + tag_length]
        if header >> 6 == 82 and tag_data[4:].startswith(b"codo/component/PlayerAccountInfo\0"):
            return tag_data
        tag_offset += tag_length
    raise AssertionError("终版主电影缺少账号栏组件")


class RechargeTests(unittest.TestCase):
    def test_original_recharge_button_and_web_event_are_preserved(self):
        project_root = Path(__file__).resolve().parents[2]
        original_movie = (project_root / "research/client/ui-investigation/codo.gfx").read_bytes()
        original_component = account_component(original_movie)
        self.assertIn(b"onReplenishCPClick", original_component)
        self.assertIn(b"EVT_SHOW_RECHARGE", original_component)
        self.assertEqual(account_component(ui_movie.patch_movie(original_movie)), original_component)
