"""从组合电影重新解析主题方法，验证局部主题值及原控制流保留。"""
from pathlib import Path
import struct
import sys
import unittest

from codol_rebuild.adapters.game.v1649370 import ui_movie

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
    def test_combined_movie_selects_five_without_dvar_call(self):
        original = (ROOT / 'client/workspaces/ui-investigation/codo.gfx').read_bytes()
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
