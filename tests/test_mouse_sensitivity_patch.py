"""核对终版鼠标灵敏度下限补丁；依赖本地原生镜像，不替代实机验收。"""
import json
import ast
from pathlib import Path
import unittest

import capstone
import pefile


class MouseSensitivityPatchTests(unittest.TestCase):
    def test_launch_allowlist_matches_runtime_manifest(self):
        client_root = Path(__file__).resolve().parents[1]
        # 静态读取启动白名单，避免导入启动器或触发原生宿主。
        syntax_tree = ast.parse((client_root / 'tools/launch_client.py').read_text(encoding='utf-8'))
        patch_assignment = next(statement for statement in syntax_tree.body
                                if isinstance(statement, ast.Assign)
                                and any(isinstance(target, ast.Name) and target.id == 'PATCH_BYTES'
                                        for target in statement.targets))
        approved_patches = ast.literal_eval(patch_assignment.value)
        patch_list = json.loads((client_root / 'resources/v1649370/offline-menu-patches.json').read_text())
        self.assertEqual([(patch['va'], patch['before'], patch['after']) for patch in patch_list],
                         list(approved_patches))

    def test_floor_bypass_preserves_surrounding_code_and_stack_cleanup(self):
        client_root = Path(__file__).resolve().parents[1]
        image_path = client_root / 'bin/v1649370/codol1649370-offline.exe'
        original_image = image_path.read_bytes()
        executable = pefile.PE(data=original_image)
        patch_list = json.loads((client_root / 'resources/v1649370/offline-menu-patches.json').read_text())
        patched_image = bytearray(original_image)
        input_patches = [patch for patch in patch_list
                         if 0x5B1770 <= int(patch['va'], 0) < 0x5B183A]
        self.assertEqual(len(input_patches), 2)
        for patch in input_patches:
            offset = executable.get_offset_from_rva(int(patch['va'], 0) - 0x400000)
            self.assertEqual(original_image[offset:offset + 1], bytes.fromhex(patch['before']))
            self.assertEqual(patch['before'], '75')
            self.assertEqual(patch['after'], 'eb')
            patched_image[offset:offset + 1] = bytes.fromhex(patch['after'])
        expected_offsets = [executable.get_offset_from_rva(address - 0x400000)
                            for address in (0x5B1809, 0x5B1816)]
        self.assertEqual([offset for offset, (before, after) in
                          enumerate(zip(original_image, patched_image)) if before != after], expected_offsets)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        for address, target in ((0x5B1809, 0x5B180E), (0x5B1816, 0x5B181D)):
            offset = executable.get_offset_from_rva(address - 0x400000)
            instruction = next(decoder.disasm(patched_image[offset:offset + 2], address))
            self.assertEqual((instruction.mnemonic, instruction.op_str), ('jmp', hex(target)))
        # 纵向分支仍执行 fstp st(0)，丢弃比较常量，避免浮点栈泄漏。
        cleanup_offset = executable.get_offset_from_rva(0x5B181D - 0x400000)
        self.assertEqual(patched_image[cleanup_offset:cleanup_offset + 2], bytes.fromhex('ddd8'))
