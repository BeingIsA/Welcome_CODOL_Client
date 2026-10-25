"""执行商城空分类保护的实际字节码，验证正常路径及原生收尾。"""
from pathlib import Path
import unittest
import zlib

from codol_rebuild.adapters.game.v1649370 import ui_movie, ui_navigation as nav


class MarketNavigationTests(unittest.TestCase):
    def normalize(self, value):
        code = nav.MARKET_ITEMS_AFTER
        stack = []
        locals_ = {0: object(), 1: value}
        pc = 0
        while pc < 23:
            op = code[pc]
            pc += 1
            if op in (0xD0, 0xD1):
                stack.append(locals_[op - 0xD0])
            elif op == 0x30:
                stack.pop()
            elif op == 0x11:
                delta = int.from_bytes(code[pc:pc + 3], 'little', signed=True)
                pc += 3
                if stack.pop() is not None:
                    pc += delta
            elif op == 0x5D:
                name = code[pc]; pc += 1
                self.assertIn(name, (2, 20))
                stack.append({3: 'Vector', 20: 'Object'})
            elif op == 0x66:
                name = code[pc]; pc += 1
                stack.append(stack.pop()[name])
            elif op == 0x53:
                self.assertEqual(code[pc], 1); pc += 1
                self.assertEqual(stack.pop(), 'Object')
                self.assertEqual(stack.pop(), 'Vector')
                stack.append('Vector<Object>')
            elif op == 0x42:
                self.assertEqual(code[pc], 0); pc += 1
                self.assertEqual(stack.pop(), 'Vector<Object>')
                stack.append([])
            elif op == 0x80:
                self.assertEqual(code[pc], 24); pc += 1
                self.assertIsInstance(stack[-1], list)
            elif op == 0xD5:
                locals_[1] = stack.pop()
            elif op != 0x02:
                self.fail(hex(op))
            self.assertLessEqual(len(stack), 3)
        self.assertEqual(pc, 23)
        self.assertEqual(stack, [])
        return locals_[1]

    def test_missing_category_becomes_empty_vector(self):
        self.assertEqual(self.normalize(None), [])
        self.assertIsNot(self.normalize(None), self.normalize(None))

    def test_empty_and_populated_categories_keep_identity(self):
        for value in ([], [{'id': 1}], [{'id': 1}, {'id': 2}]):
            self.assertIs(self.normalize(value), value)

    def test_original_product_processing_and_super_cleanup_preserved(self):
        self.assertEqual(nav.MARKET_ITEMS_AFTER[23:], nav.MARKET_ITEMS_BEFORE[23:])
        code = nav.MARKET_ITEMS_AFTER
        # 空向量 length > 0 为 false，经原有两个分支抵达 super.addItemsByVector。
        self.assertEqual(code[23:32], bytes.fromhex('d166062400af762a76'))
        for at, target in ((32, 48), (48, 134)):
            self.assertEqual(code[at], 0x12)
            self.assertEqual(at + 4 + int.from_bytes(code[at+1:at+4], 'little', signed=True), target)
        self.assertEqual(code[137:143], bytes.fromhex('d0d145280129'))

    def test_real_combined_movie_identity_and_capacity(self):
        root = Path(__file__).resolve().parents[2]
        original = (root/'research/client/ui-investigation/codo.gfx').read_bytes()
        patched = ui_movie.patch_movie(original)
        at = nav.MARKET_ITEMS_OFFSET
        self.assertEqual(original[at:at+len(nav.MARKET_ITEMS_BEFORE)], nav.MARKET_ITEMS_BEFORE)
        self.assertEqual(patched[at:at+len(nav.MARKET_ITEMS_AFTER)], nav.MARKET_ITEMS_AFTER)
        self.assertEqual(len(patched), len(original))
        self.assertLessEqual(len(zlib.compress(patched, 9)), ui_movie.COMPRESSED_SIZE)


if __name__ == '__main__':
    unittest.main()
