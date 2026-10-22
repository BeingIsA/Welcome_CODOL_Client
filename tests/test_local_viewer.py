"""核对本地人物预览适配的原生调用边、寄存器保护和失败分支。"""
from pathlib import Path
import unittest

import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.viewer import (
    build_local_viewer_adapter, PATCH_VA, BODY_VA, EXIT_VA, GATE_BYTES,
    VIEWER, INVENTORY_READY, INVENTORY_COUNT, WEAPON_COUNT, DW_POINTER,
)


class LocalViewerTests(unittest.TestCase):
    def test_native_image_gate_and_exit_contract(self):
        root = Path(__file__).resolve().parents[2]
        pe = pefile.PE(str(root/'client/bin/v1649370/codol1649370-offline.exe'))
        self.assertEqual(pe.get_data(PATCH_VA-0x400000, len(GATE_BYTES)), GATE_BYTES)
        self.assertEqual(PATCH_VA+len(GATE_BYTES), BODY_VA)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        gate = list(decoder.disasm(GATE_BYTES, PATCH_VA))
        self.assertEqual([i.op_str for i in gate if i.mnemonic=='call'],
                         ['0x730b00', '0x50f070', '0x7f9090'])
        self.assertTrue(all(int(i.op_str,16)==EXIT_VA for i in gate if i.mnemonic in ('je','jne') and int(i.op_str,16)>BODY_VA))
        # 原生 epilogue 自行弹出 EDI、ESI、EBX 并 RET 4；适配器不可提前返回。
        self.assertEqual(pe.get_data(EXIT_VA-0x400000, 19).hex(),
                         '8b4df85f5e33cd5be83a2e62008be55dc20400')

    def test_gates_restore_context_and_use_native_body_or_exit(self):
        for entry in (0x20000000, 0xf0000000):
            with self.subTest(entry=hex(entry)):
                ready = 0x21000048
                result = build_local_viewer_adapter(entry, ready)
                decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
                rows = list(decoder.disasm(result['code'],entry))
                pairs = [(i.mnemonic,i.op_str) for i in rows]
                self.assertEqual(sum(i.size for i in rows),len(result['code']))
                self.assertEqual(pairs[:2],[('pushfd',''),('pushal','')])
                self.assertEqual(pairs[-6:], [('popal',''),('popfd',''),('jmp',hex(BODY_VA)),
                                              ('popal',''),('popfd',''),('jmp',hex(EXIT_VA))])
                denied=rows[-3].address
                for i in rows:
                    if i.mnemonic.startswith('j') and i.mnemonic!='jmp':
                        self.assertIn(int(i.op_str,16),(denied,rows[-6].address))
                for address in (ready, INVENTORY_READY, VIEWER):
                    self.assertIn(('cmp',f'byte ptr [{address:#x}], 1'),pairs)
                for address in (INVENTORY_COUNT, WEAPON_COUNT):
                    pos=pairs.index(('cmp',f'dword ptr [{address:#x}], 0'))
                    self.assertEqual(pairs[pos+1][0],'jle')
                pos=pairs.index(('test','eax, eax'))
                self.assertEqual(pairs[pos+1][0],'je')
                self.assertLess(pos,pairs.index(('cmp','dword ptr [eax + 0xc], 0')))
                self.assertIn(('cmp',f'ebx, {VIEWER:#x}'),pairs)
                self.assertIn(('mov','eax, dword ptr [ebx + 0x84]'),pairs)
                self.assertNotIn(('mov','eax, dword ptr [ebx + 8]'),pairs)
                self.assertNotIn('call',[i.mnemonic for i in rows])
                patch=list(decoder.disasm(result['after'],PATCH_VA))
                self.assertEqual((patch[0].mnemonic,int(patch[0].op_str,16)),('jmp',entry))

    def test_team_requires_data_and_unknown_modes_rejected(self):
        result=build_local_viewer_adapter(0x20000000,0x21000048)
        rows=list(capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32).disasm(result['code'],0x20000000))
        pairs=[(i.mnemonic,i.op_str) for i in rows]
        pos=pairs.index(('cmp','eax, 0xa'))
        self.assertEqual(pairs[pos+1][0],'ja')
        pos=pairs.index(('cmp','eax, 3'))
        self.assertEqual(pairs[pos+1][0],'jne')
        self.assertIn(('cmp','dword ptr [eax + 0x105d8], 0x12'),pairs)
        self.assertIn(3,result['local_modes'])
        self.assertNotIn(11,result['local_modes'])
