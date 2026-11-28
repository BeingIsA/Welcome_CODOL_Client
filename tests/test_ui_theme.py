"""从组合电影重新解析主题方法，验证局部主题值及原控制流保留。"""
from pathlib import Path
import struct
import sys
import unittest

from codol_rebuild.adapters.game.v1649370.session import ui_movie

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'research'))
from trace_matchmaking_debugline import Reader, abc_parse, disassemble


def theme_method(movie):
    reader = Reader(movie)
    reader.p = 8 + (5 + 4 * (movie[8] >> 3) + 7) // 8 + 4
    while reader.p < len(movie):
        header = struct.unpack('<H', reader.raw(2))[0]
        kind, size = header >> 6, header & 63
        if size == 63:
            size = struct.unpack('<I', reader.raw(4))[0]
        payload = reader.raw(size)
        if kind != 82:
            continue
        item = Reader(payload)
        item.raw(4)
        if item.cstr() != 'codo/controllers/ThemeController':
            continue
        methods, names, strings = abc_parse(payload[item.p:])
        method = next(m for m in methods if 'code' in m
                      and 'loadThemeConfigure' in m.get('name', ''))
        return method, disassemble(method, names, strings)
    raise AssertionError('主题控制器缺失')


class ThemeTests(unittest.TestCase):
    def test_root_watermark_is_readonly_and_has_no_button_hit_area(self):
        original = (ROOT/'research/client/ui-investigation/codo.gfx').read_bytes()
        patched = ui_movie.patch_movie(original)
        cursor = len(original) - 4
        tags = []
        while cursor < len(patched):
            header, = struct.unpack_from('<H', patched, cursor)
            cursor += 2
            length = header & 63
            if length == 63:
                length, = struct.unpack_from('<I', patched, cursor)
                cursor += 4
            tags.append((header >> 6, patched[cursor:cursor + length]))
            cursor += length
        self.assertEqual(cursor, len(patched))
        self.assertEqual([kind for kind, _ in tags], [37, 34, 26, 1, 0])
        text_definition, button_definition, placement = (entry[1] for entry in tags[:3])
        text_identifier, = struct.unpack_from('<H', text_definition)
        rectangle_bits = 5 + 4 * (text_definition[2] >> 3)
        flags_offset = 2 + (rectangle_bits + 7) // 8
        self.assertEqual(text_definition[flags_offset:flags_offset + 2], b'\x8c\xb1')
        color_offset = text_definition.index(b'$font_3\0', flags_offset) + len(b'$font_3\0') + 2
        self.assertEqual(text_definition[color_offset:color_offset + 5], bytes((255, 255, 255, 190, 0)))
        self.assertTrue(text_definition.endswith('Welcome CODOL 完全免费，严禁用于商业用途\0'.encode()))
        button_identifier, _, action_offset, states, child_identifier = struct.unpack_from(
            '<HBHBH', button_definition)
        self.assertEqual((action_offset, states, child_identifier), (0, 7, text_identifier))
        # 唯一的文字实例使用原坐标、原颜色，随后结束按钮记录。
        self.assertEqual(struct.unpack_from('<H', button_definition, 8), (1,))
        self.assertEqual(button_definition[10:], b'\0\0\0')
        self.assertEqual(struct.unpack_from('<BHH', placement), (6, 1, button_identifier))

    def test_mini_client_warning_skips_only_popup_and_keeps_member_counter(self):
        """保留短路分支的落点及栈状态，仅移除整个零净栈变化的弹窗表达式。"""
        from codol_rebuild.adapters.game.v1649370.frontend import ui_theme
        original = (ROOT / 'research/client/ui-investigation/codo.gfx').read_bytes()
        patched = ui_movie.patch_movie(original)
        at = ui_theme.MINI_WARNING_OFFSET
        end = at+len(ui_theme.MINI_WARNING_BEFORE)
        self.assertEqual(original[at:end], ui_theme.MINI_WARNING_BEFORE)
        self.assertEqual(patched[at:end], b'\x02'*26)
        start = 5101244
        self.assertEqual(original[start:at], patched[start:at])
        self.assertEqual(original[end:start+212], patched[end:start+212])
        # 两条短路分支直接落到 iffalse，不能只在前一条 convert_b 消费条件。
        for branch in (134, 149):
            target = branch+4+int.from_bytes(patched[start+branch+1:start+branch+4], 'little')
            self.assertEqual(target, 159)
            self.assertEqual(patched[start+target], 0x12)

    def test_combined_movie_selects_five_without_dvar_call(self):
        original = (ROOT / 'research/client/ui-investigation/codo.gfx').read_bytes()
        before, _ = theme_method(original)
        after, rows = theme_method(ui_movie.patch_movie(original))
        self.assertEqual(before['limits'], after['limits'])
        self.assertEqual(len(before['code']), len(after['code']))
        stack = []
        for row in rows:
            op, args = row['instruction'], row['args']
            if op in ('debug', 'debugline', 'debugfile', 'nop'):
                continue
            if op == 'getlocal0':
                stack.append(object())
            elif op == 'pushscope':
                stack.pop()
            elif op == 'pushbyte':
                stack.append(args[0])
            elif op == 'setlocal1':
                self.assertEqual(stack.pop(), 5)
                self.assertEqual(stack, [])
                break
            else:
                self.fail(f'主题赋值仍依赖外部调用：{op}')
        else:
            self.fail('缺少主题赋值')
        # 赋值后的范围检查、选取主题及资源断言保持原样。
        self.assertEqual(bytes.fromhex(before['code'])[22:],
                         bytes.fromhex(after['code'])[22:])
