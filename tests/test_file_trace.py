"""独立反汇编核对门桩分支、参数顺序及 32 位地址边界；不启动游戏。"""
import unittest

from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from codol_rebuild.runtime.file_trace import build_file_gate


class FileGateTests(unittest.TestCase):
    def decode(self, entry=0x20000000, original=0x70000100,
               get_error=0x70000200, set_error=0x70000300):
        code, trap = build_file_gate(entry, original, get_error, set_error)
        rows = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(code, entry))
        self.assertEqual(sum(row.size for row in rows), len(code))
        return code, trap, rows

    def test_decoded_argument_order_and_stdcall(self):
        """七个参数副本按右到左转发；原参数在最终 ret 28 时一次清理。"""
        _, _, rows = self.decode()
        pushes = [row.op_str for row in rows if row.mnemonic == 'push']
        self.assertEqual(pushes, ['ebp'] + 2 * [f'dword ptr [ebp + {n}]' for n in
                         ('0x20', '0x1c', '0x18', '0x14', '0x10', '0xc', '8')]
                         + ['dword ptr [ebp - 8]'])
        self.assertEqual([(r.mnemonic, r.op_str) for r in rows[-4:]],
                         [('mov', 'eax, dword ptr [ebp - 4]'), ('mov', 'esp, ebp'),
                          ('pop', 'ebp'), ('ret', '0x1c')])
        self.assertFalse(any(reg in r.op_str for r in rows for reg in ('ebx', 'esi', 'edi')))

    def test_real_decoded_call_targets_and_branches(self):
        """Capstone 独立解析跳转落点，避免位移回填导致正常打开也命中。"""
        code, trap, rows = self.decode()
        self.assertEqual([int(r.op_str, 16) for r in rows if r.mnemonic == 'call'],
                         [0x70000100, 0x70000200, 0x70000100, 0x70000200, 0x70000300])
        finish = next(r.address for r in rows if r.mnemonic == 'push' and r.op_str == 'dword ptr [ebp - 8]')
        self.assertEqual([(r.mnemonic, int(r.op_str, 16)) for r in rows
                          if r.mnemonic in ('jne', 'je')],
                         [('jne', finish), ('je', trap-7), ('jne', finish), ('jne', finish)])
        self.assertEqual(code[trap-0x20000000], 0xcc)
        self.assertEqual(sum(r.mnemonic == 'int3' for r in rows), 1)

    def test_call_displacement_crosses_signed_boundary(self):
        """x86 rel32 按模 2^32 计算，合法低地址到高地址调用不可误拒绝。"""
        _, _, rows = self.decode(entry=0x1000, original=0xf0000000,
                                 get_error=0x90000000, set_error=0x80001000)
        self.assertEqual([int(r.op_str, 16) for r in rows if r.mnemonic == 'call'],
                         [0xf0000000, 0x90000000, 0xf0000000, 0x90000000, 0x80001000])

    def test_wide_does_not_dereference_untrusted_filename(self):
        """A/W 只影响宿主解码；两种门不读取路径内存。"""
        args = (0x20000000, 0x70000100, 0x70000200, 0x70000300)
        self.assertEqual(build_file_gate(*args), build_file_gate(*args, wide=True))

    def test_bad_addresses_types_wrap_and_recursion_fail(self):
        """拒绝截断宿主指针、空地址、代码回绕及门内自调用。"""
        values = [0x20000000, 0x70000100, 0x70000200, 0x70000300]
        for index in range(4):
            for bad in (0, -1, 0x100000000, True, 1.0, None):
                args = values.copy(); args[index] = bad
                with self.subTest(index=index, bad=bad), self.assertRaises(ValueError):
                    build_file_gate(*args)
        with self.assertRaises(ValueError):
            build_file_gate(0xfffffff0, *values[1:])
        for index in (1, 2, 3):
            args = values.copy(); args[index] = args[0] + 4
            with self.assertRaises(ValueError):
                build_file_gate(*args)
        with self.assertRaises(ValueError):
            build_file_gate(*values, wide=1)


if __name__ == '__main__':
    unittest.main()
