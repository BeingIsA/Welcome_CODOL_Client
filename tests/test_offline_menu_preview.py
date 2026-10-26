"""独立反汇编核对 UI 调用桩边界和 ABI；不执行游戏代码。"""
import unittest

from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_AC_WRITE
from capstone.x86_const import X86_OP_MEM
from codol_rebuild.adapters.game.v1649370.frontend.menu_preview import (
    ACTIVATE_VA, GETTER_VA, PATCH_VA, RETURN_VA, FIELDS,
    build_menu_preview_helper, validate_menu_preview_image,
    PreviewActivationCycles,
)


class MenuPreviewTests(unittest.TestCase):
    def test_native_reset_allows_new_cycle_but_not_retry_or_reentry(self):
        cycles = PreviewActivationCycles()
        self.assertEqual(cycles.begin(0), 1)
        with self.assertRaises(ValueError):
            cycles.begin(0)
        cycles.complete(1, 1)
        with self.assertRaises(ValueError):
            cycles.begin(1)
        self.assertEqual(cycles.begin(0), 2)
        cycles.complete(1, 1)
        self.assertEqual(cycles.begin(0), 3)
        cycles.complete(0, 1)
        with self.assertRaises(ValueError):
            cycles.begin(0)

    def test_return_requires_pending_activation_and_native_once(self):
        cycles = PreviewActivationCycles()
        with self.assertRaises(ValueError):
            cycles.complete(1, 1)
        cycles.begin(0)
        with self.assertRaises(ValueError):
            cycles.complete(1, 0)

    def test_rebuilt_movie_skips_completed_profile_initializer(self):
        base, initializer, ready = 0x20000000, 0x21000000, 0x22000048
        result = build_menu_preview_helper(base, initializer_va=initializer, initializer_ready_va=ready)
        rows = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(result['entry_bytes'], base))
        pairs = [(r.mnemonic, r.op_str) for r in rows]
        index = pairs.index(('cmp', f'dword ptr [{ready:#x}], 1'))
        self.assertEqual(pairs[index+1][0], 'je')
        activation = next(r.address for r in rows if r.mnemonic=='call' and int(r.op_str,16)==ACTIVATE_VA)
        self.assertEqual(int(pairs[index+1][1],16), activation)
        self.assertEqual(pairs[index+2], ('call', hex(initializer)))
        self.assertGreater(rows[index].address, base+result['before_trap_offset'])
        self.assertEqual(sum(r.size for r in rows), len(result['entry_bytes']))

    def test_local_activation_waits_for_real_weapon_data_before_initializer(self):
        address=0x20000000
        result=build_menu_preview_helper(address,initializer_va=0x21000000)
        rows=list(Cs(CS_ARCH_X86,CS_MODE_32).disasm(result['entry_bytes'],address))
        pairs=[(r.mnemonic,r.op_str) for r in rows]
        finish=hex(address+result['finish_offset'])
        before=address+result['before_trap_offset']
        for instruction,branch in [(('cmp','byte ptr [0xc7341d8], 1'),'jne'),
                                    (('cmp','dword ptr [0x827f738], 0'),'jle')]:
            index=pairs.index(instruction)
            self.assertEqual(pairs[index+1],(branch,finish))
            self.assertLess(rows[index].address,before)
        self.assertEqual([int(r.op_str,16) for r in rows if r.mnemonic=='call'],
                         [GETTER_VA,0x21000000,ACTIVATE_VA])
        self.assertEqual(sum(r.size for r in rows),len(result['entry_bytes']))

    def decode(self, address=0x20000000):
        result = build_menu_preview_helper(address)
        decoder = Cs(CS_ARCH_X86, CS_MODE_32)
        decoder.detail = True
        rows = list(decoder.disasm(result['entry_bytes'], address))
        self.assertEqual(sum(row.size for row in rows), len(result['entry_bytes']))
        return result, rows

    def test_low_and_high_allocation_call_targets(self):
        """相隔超过有符号2GB的原CALL和内部CALL仍按x86模32正确到达。"""
        for address in (0x10000, 0x20000000, 0x90000000, 0xf0000000):
            with self.subTest(address=hex(address)):
                result, rows = self.decode(address)
                self.assertEqual([int(r.op_str, 16) for r in rows if r.mnemonic == 'call'],
                                 [GETTER_VA, ACTIVATE_VA])
                patch = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(result['patch_after'], PATCH_VA))
                self.assertEqual([(r.mnemonic, int(r.op_str, 16)) for r in patch], [('call', address)])
                original = list(Cs(CS_ARCH_X86, CS_MODE_32).disasm(result['patch_before'], PATCH_VA))
                self.assertEqual(int(original[0].op_str, 16), GETTER_VA)

    def test_getter_alignment_and_saved_frame(self):
        """LEA 保留原 getter flags；完整保存帧与原始返回地址偏移独立演算。"""
        result, rows = self.decode()
        self.assertEqual([(r.mnemonic, r.op_str) for r in rows[:6]], [
            ('lea', 'esp, [esp - 0xc]'), ('call', hex(GETTER_VA)),
            ('lea', 'esp, [esp + 0xc]'), ('pushfd', ''), ('pushal', ''), ('mov', 'ebp, esp')])
        for original_esp in range(0x10000, 0x10010):
            getter_esp = original_esp - 12 - 4
            self.assertEqual(getter_esp % 16, original_esp % 16)
            frame = original_esp - 4 - 32
            self.assertEqual(frame + result['metadata']['saved_frame']['original_return'], original_esp)
        self.assertEqual(result['metadata']['saved_frame'], {
            'edi': 0, 'esi': 4, 'ebp': 8, 'pushad_esp': 12, 'ebx': 16,
            'edx': 20, 'ecx': 24, 'getter_eax': 28, 'getter_eflags': 32, 'original_return': 36})
        self.assertEqual(result['metadata']['return_va'], RETURN_VA)

    def test_each_native_precondition_width_and_failure_branch(self):
        """验证固定字段读取宽度和比较方向，避免 DW 指针或目录负数误通过。"""
        result, rows = self.decode()
        pairs = [(r.mnemonic, r.op_str) for r in rows]
        finish = hex(result['entry_va'] + result['finish_offset'])
        def gate(instruction, branch):
            index = pairs.index(instruction)
            self.assertEqual(pairs[index + 1], (branch, finish))
        gate(('cmp', 'eax, 1'), 'jne')
        gate(('test', 'eax, eax'), 'je')
        gate(('cmp', 'dword ptr [eax + 0xc], 0'), 'jne')
        gate(('cmp', 'byte ptr [0x84abb2f], 1'), 'jne')
        gate(('cmp', 'byte ptr [0xe7b630b], 1'), 'jne')
        gate(('cmp', 'dword ptr [0x7795840], 0'), 'jle')
        for address in (0x779583c, 0xc7ae9a0, 0xc77bb98, 0xe69740, 0xe69748):
            gate(('cmp', f'dword ptr [{address:#x}], 0'), 'je')
        gate(('cmp', 'byte ptr [0x82e1344], 0'), 'jne')
        self.assertLess(pairs.index(('test', 'eax, eax')), pairs.index(('cmp', 'dword ptr [eax + 0xc], 0')))

    def test_managed_gfx_type_and_branch_targets(self):
        """仅 managed Object/DisplayObject 到达调用边；所有分支落在完整指令上。"""
        result, rows = self.decode()
        pairs = [(r.mnemonic, r.op_str) for r in rows]
        start = pairs.index(('test', 'al, 0x40'))
        sequence = pairs[start:start + 8]
        finish = hex(result['entry_va'] + result['finish_offset'])
        interface_check = next(r.address for r in rows if r.op_str == 'dword ptr [0xe69740], 0')
        self.assertEqual(sequence, [('test', 'al, 0x40'), ('je', finish), ('and', 'eax, 0x8f'),
                                   ('cmp', 'eax, 8'), ('je', hex(interface_check)),
                                   ('cmp', 'eax, 0xa'), ('jne', finish), ('cmp', 'dword ptr [0xe69740], 0')])
        addresses = {r.address for r in rows}
        for row in rows:
            if row.mnemonic.startswith('j'):
                self.assertIn(int(row.op_str, 16), addresses)
                self.assertGreater(int(row.op_str, 16), row.address)
        self.assertEqual(result['metadata']['accepted_root_base_types'], [8, 10])

    def test_traps_keep_native_al_and_restore_getter_state(self):
        """后置陷阱前仅 MOV 恢复 ESP，不改变原生 AL；最终无条件恢复 getter 现场。"""
        result, rows = self.decode()
        pairs = [(r.mnemonic, r.op_str) for r in rows]
        before = next(i for i, r in enumerate(rows) if r.address == result['entry_va'] + result['before_trap_offset'])
        self.assertEqual(pairs[before:], [('int3', ''), ('and', 'esp, 0xfffffff0'),
                                         ('call', hex(ACTIVATE_VA)), ('mov', 'esp, ebp'), ('int3', ''),
                                         ('mov', 'esp, ebp'), ('popal', ''), ('popfd', ''), ('ret', '')])
        self.assertEqual(rows[before + 4].address, result['entry_va'] + result['after_trap_offset'])
        self.assertEqual(result['metadata']['trap_continue'], 'EIP=trap+1')
        self.assertTrue(result['metadata']['trap_esp_equals_frame'])
        self.assertEqual(result['metadata']['native_return_at_after_trap'], 'AL')

    def test_no_explicit_global_memory_writes(self):
        """桩自身只读取游戏字段，状态变化仅可能来自明确调用的原生 UI 方法。"""
        result, rows = self.decode()
        for row in rows:
            for operand in row.operands:
                self.assertFalse(operand.type == X86_OP_MEM and operand.access & CS_AC_WRITE,
                                 f'{row.mnemonic} {row.op_str}')
        self.assertFalse(result['metadata']['writes_online_or_profile_state'])
        self.assertFalse(result['metadata']['game_execution_verified'])

    def test_bad_address_and_base_fingerprint_rejected(self):
        for address in (0, -1, 0x100000000, True, 1.0, None, 0xffffff80,
                        PATCH_VA - 1, GETTER_VA, ACTIVATE_VA, *FIELDS.values()):
            with self.subTest(address=address), self.assertRaises(ValueError):
                build_menu_preview_helper(address)
        for image in (b'', b'MZ', bytearray(b'MZ'), None):
            with self.subTest(image=image), self.assertRaises(ValueError):
                validate_menu_preview_image(image)


if __name__ == '__main__':
    unittest.main()
