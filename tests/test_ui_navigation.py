"""验证真实原版 ABC 的空值分支、压缩资源边界与固定加载调用边。"""
from pathlib import Path
import struct
import unittest
import zlib
import pefile
from codol_rebuild.adapters.game.v1649370 import ui_navigation as nav

ROOT = Path(__file__).resolve().parents[2]


def evaluate_date(code, start, season, now):
    """执行日期判断实际 AVM2 指令，检查短路分支及栈平衡，不模拟 UI。"""
    stack = []
    pc = start
    def u30():
        nonlocal pc
        value = shift = 0
        while True:
            byte = code[pc]; pc += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
    while pc < 127:
        op = code[pc]; pc += 1
        if op == 0x27: stack.append(False)
        elif op == 0xD2: stack.append(season)
        elif op == 0x62:
            assert u30() == 4
            stack.append(now)
        elif op == 0x66:
            key = {144: 'startTimeStamp', 145: 'endTimeStamp'}[u30()]
            stack.append(stack.pop()[key])
        elif op in (0xB0, 0xAE):
            right = stack.pop(); left = stack.pop()
            stack.append(left >= right if op == 0xB0 else left <= right)
        elif op == 0x76: stack[-1] = bool(stack[-1])
        elif op == 0x2A: stack.append(stack[-1])
        elif op == 0x29: stack.pop()
        elif op == 0x12:
            distance = int.from_bytes(code[pc:pc+3], 'little', signed=True); pc += 3
            if not stack.pop(): pc += distance
        elif op == 0x63:
            assert u30() == 5 and len(stack) == 1
            return stack.pop()
        else: raise AssertionError((pc-1, op))
    raise AssertionError('没有到达原版布尔变量赋值')


class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (ROOT/'client/workspaces/ui-investigation/codo.gfx').read_bytes()
        cls.patched = nav.patch_movie(cls.original)
        cls.code = cls.patched[nav.METHOD_OFFSET:nav.METHOD_OFFSET+200]
        zone = (ROOT/'client/workspaces/ui-investigation/ui_mp.zone').read_bytes()
        start = zone.index(nav.RESOURCE_NAME) + len(nav.RESOURCE_NAME)
        cls.compressed = zone[start:start+nav.COMPRESSED_SIZE]

    def test_null_short_circuit(self):
        self.assertFalse(evaluate_date(self.code, 94, None, 10))
        with self.assertRaises(TypeError):
            evaluate_date(nav.ORIGINAL_METHOD, 101, None, 10)

    def test_room_without_playlist_skips_lookup_and_keeps_default(self):
        """核对真实方法分支：缺少编号直达默认倍率，有编号保留原读取路径。"""
        start = nav.ROOM_DETAIL_OFFSET
        before = self.original[start:start+72]
        after = self.patched[start:start+72]
        self.assertEqual(before, nav.ROOM_DETAIL_BEFORE)
        self.assertEqual(len(nav.ROOM_DETAIL_AFTER), 72)
        # 把原先读取之后的 undefined 比较前移，ifeq 跳到原默认赋值。
        self.assertEqual(after[:11], before[57:68])
        self.assertEqual(after[11], 0x13)
        target = 15 + int.from_bytes(after[12:15], 'little', signed=True)
        self.assertEqual(target, 72)
        self.assertEqual(after[15:66], before[3:54])
        # 有编号读取完后跳过默认赋值；默认代码和后续显示代码未修改。
        self.assertEqual(after[66], 0x10)
        target = 70 + int.from_bytes(after[67:70], 'little', signed=True)
        self.assertEqual(target, 90)
        self.assertEqual(self.patched[start+72:start+325], self.original[start+72:start+325])

    def test_nonnull_matches_original_at_boundaries(self):
        for first, last in ((10, 20), (0, 0), (20, 10), (-100, 100)):
            season = {'startTimeStamp': first, 'endTimeStamp': last}
            for now in (-101, -1, 0, 9, 10, 11, 19, 20, 21, 101):
                with self.subTest(first=first, last=last, now=now):
                    self.assertEqual(evaluate_date(self.code,94,season,now),
                                     evaluate_date(nav.ORIGINAL_METHOD,101,season,now))

    def test_only_registered_navigation_spans_changed(self):
        self.assertEqual(len(self.patched), len(self.original))
        end = 0
        for offset, before, after in sorted(nav.replacements()):
            self.assertEqual(self.patched[end:offset], self.original[end:offset])
            self.assertEqual(self.patched[offset:offset + len(after)], after)
            end = offset + len(before)
        self.assertEqual(self.patched[end:], self.original[end:])
        # 原版日期比较、锁定状态检查、按钮更新及所有下游调用逐字节保持。
        self.assertEqual(self.code[101:], nav.ORIGINAL_METHOD[101:])

    def test_reject_changed_or_truncated_resource(self):
        for bad in (self.original[:-1], b'X'+self.original[1:], self.patched):
            with self.assertRaises(ValueError): nav.patch_movie(bad)

    def memory(self, *, bad_name=False):
        header = struct.pack('<4I',0x2000,nav.COMPRESSED_SIZE,nav.RESOURCE_SIZE,0x3000)
        blocks = {0x1000:header, 0x2000:b'X'*len(nav.RESOURCE_NAME) if bad_name else nav.RESOURCE_NAME,
                  0x3000:self.compressed}
        def read(address,size):
            for base, block in blocks.items():
                offset = address-base
                if 0 <= offset and offset+size <= len(block):return block[offset:offset+size]
            raise OSError('夹具范围之外')
        return read

    def test_real_compressed_resource_and_scan(self):
        plan = nav.find_resource_patch(self.memory(),[(0x1000,16)])
        self.assertEqual(plan['header_address'],0x1000)
        self.assertEqual(zlib.decompress(plan['data_after']),self.patched)
        self.assertLessEqual(len(plan['data_after']),nav.COMPRESSED_SIZE)
        self.assertEqual(struct.unpack('<I',plan['length_after'])[0],len(plan['data_after']))

    def test_reject_wrong_name_and_missing_resource(self):
        with self.assertRaises(ValueError):nav.build_resource_patch(self.memory(bad_name=True),0x1000)
        with self.assertRaises(ValueError):nav.find_resource_patch(self.memory(),[])

    def test_reject_ambiguous_resource(self):
        read = self.memory()
        def duplicate(address,size):
            return read(0x1000,size) if address == 0x5000 else read(address,size)
        with self.assertRaises(ValueError):
            nav.find_resource_patch(duplicate,[(0x1000,16),(0x5000,16)])

    def test_fixed_load_site(self):
        pe = pefile.PE(str(ROOT/'client/bin/v1649370/codol1649370-offline.exe'))
        self.assertEqual(pe.get_data(nav.LOAD_SITE-0x400000,5),nav.LOAD_BYTES)


if __name__ == '__main__':
    unittest.main()
