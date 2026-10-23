"""验证离线净化短路的两处等长替换、组合顺序与压缩边界。"""
from pathlib import Path
import struct
import unittest
import zlib

from codol_rebuild.adapters.game.v1649370 import player_name, sanitize, ui_movie, ui_navigation

ROOT = Path(__file__).resolve().parents[2]


class SanitizePatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (ROOT/'client/workspaces/ui-investigation/codo.gfx').read_bytes()
        cls.patched = ui_movie.patch_movie(cls.original)
        zone = (ROOT/'client/workspaces/ui-investigation/ui_mp.zone').read_bytes()
        start = zone.index(ui_movie.RESOURCE_NAME) + len(ui_movie.RESOURCE_NAME)
        cls.compressed = zone[start:start+ui_movie.COMPRESSED_SIZE]

    def memory(self):
        header = struct.pack('<4I',0x2000,ui_movie.COMPRESSED_SIZE,ui_movie.RESOURCE_SIZE,0x3000)
        blocks = {0x1000:header,0x2000:ui_movie.RESOURCE_NAME,0x3000:self.compressed}
        def read(address,size):
            for base,block in blocks.items():
                offset = address-base
                if 0 <= offset and offset+size <= len(block):
                    return block[offset:offset+size]
            raise OSError('夹具范围之外')
        return read

    def test_two_methods_replaced_in_place(self):
        self.assertEqual(len(self.patched),len(self.original))
        tail = sanitize.SANITIZE_OFFSET+sanitize.SANITIZE_LENGTH-len(sanitize.SANITIZE_TAIL_BEFORE)
        self.assertEqual(self.patched[tail:tail+len(sanitize.SANITIZE_TAIL_AFTER)],
                         sanitize.SANITIZE_TAIL_AFTER)
        self.assertEqual(self.patched[sanitize.LOADOUT_OFFSET:
                                       sanitize.LOADOUT_OFFSET+sanitize.LOADOUT_LENGTH],
                         sanitize.LOADOUT_AFTER)

    def test_only_registered_spans_changed(self):
        spans = [(offset,offset+len(before)) for offset,before,_ in ui_movie.replacements()]
        for index,byte in enumerate(self.patched):
            if any(start <= index < end for start,end in spans):
                continue
            self.assertEqual(byte,self.original[index],f'{index:#x} 越界改动')

    def test_sanitize_request_replaced_by_broadcast(self):
        self.assertNotIn(b'\x46\x28\x03',sanitize.SANITIZE_TAIL_AFTER)  # 原 request(...,3)
        self.assertIn(b'\x46\x11\x02',sanitize.SANITIZE_TAIL_AFTER)     # broadcast(...,2)
        self.assertIn(b'\x5d\x02\x66\x02\x66\x0f',sanitize.SANITIZE_TAIL_AFTER)

    def test_loadout_parameter_carries_sanitized(self):
        self.assertIn(b'\x2a\x27',sanitize.LOADOUT_AFTER)      # dup; pushfalse（未过滤）
        self.assertIn(b'\x61\xbb\x01',sanitize.LOADOUT_AFTER)  # setproperty sanitized
        self.assertIn(b'\x55\x02',sanitize.LOADOUT_AFTER)      # newobject 2
        self.assertTrue(sanitize.LOADOUT_AFTER.rstrip(b'\x02').endswith(b'\x47'))

    def test_rules_cover_combined_methods_without_overlap(self):
        rules = ui_movie.replacements()
        # 七个规则提供者合计 21 条：导航 4、净化 2、改名 4、主题 1、周常 3、充值 4、购买 3。
        self.assertEqual(len(rules),21)
        spans = sorted((offset,offset+len(before)) for offset,before,_ in rules)
        for (_,end),(next_start,_) in zip(spans,spans[1:]):
            self.assertLessEqual(end,next_start,'替换区间重叠')

    def test_tcls_check_replaced_by_true(self):
        start = player_name.TCLS_CHECK_OFFSET
        end = start+len(player_name.TCLS_CHECK_AFTER)
        self.assertEqual(self.patched[start:end],player_name.TCLS_CHECK_AFTER)
        self.assertEqual(self.patched[start],0x26)                      # pushtrue
        self.assertEqual(self.patched[end:end+4],bytes.fromhex('12300000'))  # 原 iffalse 保留
        self.assertEqual(self.patched[end-1],0x02)                      # nop 填充

    def test_movie_patch_keeps_navigation_fix(self):
        nav_only = ui_navigation.patch_movie(self.original)
        self.assertEqual(self.patched[ui_navigation.METHOD_OFFSET:
                                      ui_navigation.METHOD_OFFSET+len(ui_navigation.ORIGINAL_METHOD)],
                         nav_only[ui_navigation.METHOD_OFFSET:
                                  ui_navigation.METHOD_OFFSET+len(ui_navigation.ORIGINAL_METHOD)])

    def test_reject_changed_truncated_or_patched_resource(self):
        for bad in (self.original[:-1],b'X'+self.original[1:],self.patched):
            with self.assertRaises(ValueError):
                ui_movie.patch_movie(bad)

    def test_reject_shifted_method_bytes(self):
        shifted = bytearray(self.original)
        shifted[sanitize.LOADOUT_OFFSET] ^= 0xFF
        with self.assertRaises(ValueError):
            ui_movie.patch_movie(bytes(shifted))

    def test_compressed_plan_stays_within_original_buffer(self):
        plan = ui_movie.find_resource_patch(self.memory(),[(0x1000,16)])
        self.assertEqual(plan['header_address'],0x1000)
        self.assertEqual(zlib.decompress(plan['data_after']),self.patched)
        self.assertLessEqual(len(plan['data_after']),ui_movie.COMPRESSED_SIZE)
        self.assertEqual(struct.unpack('<I',plan['length_after'])[0],len(plan['data_after']))

    def test_navigation_scan_still_only_applies_hall_fix(self):
        plan = ui_navigation.find_resource_patch(self.memory(),[(0x1000,16)])
        self.assertEqual(zlib.decompress(plan['data_after']),ui_navigation.patch_movie(self.original))


if __name__ == '__main__':
    unittest.main()
