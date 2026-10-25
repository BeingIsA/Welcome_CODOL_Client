"""房间名桥的固定版本证据和跳转边界检查；执行 ABI 另由原生夹具覆盖。"""
from pathlib import Path
import unittest
import capstone
import pefile
from codol_rebuild.adapters.game.v1649370.room_name import (
    build_room_name_adapter, validate_room_name_contract, ORIGINAL_VA, CALLBACK_VA,
)


class RoomNameTests(unittest.TestCase):
    def test_actual_image_contract(self):
        root = Path(__file__).resolve().parents[2]
        pe = pefile.PE(str(root/'client/bin/v1649370/codol1649370-offline.exe'))
        validate_room_name_contract(lambda a,n: pe.get_data(a-0x400000,n))

    def test_wrong_image_refused(self):
        with self.assertRaises(ValueError):
            validate_room_name_contract(lambda a,n: bytes(n))

    def test_control_flow_targets_at_low_and_high_allocation(self):
        decoder = capstone.Cs(capstone.CS_ARCH_X86,capstone.CS_MODE_32)
        for entry in (0x20000000,0xf0000000):
            code = build_room_name_adapter(entry,0x21000000)['code']
            rows = list(decoder.disasm(code,entry))
            self.assertEqual(sum(i.size for i in rows),len(code))
            starts = {i.address for i in rows}
            self.assertEqual([int(i.op_str,16) for i in rows if i.mnemonic=='call'],[CALLBACK_VA])
            for i in rows:
                if i.mnemonic.startswith('j'):
                    self.assertIn(int(i.op_str,16),starts | {ORIGINAL_VA})

    def test_task_callback_is_deferred_by_original_manager(self):
        root = Path(__file__).resolve().parents[2]/'research/client/ui-investigation/source/scripts/codo/manager'
        source = (root/'AsyncTaskManager.as').read_text(encoding='utf-8')
        self.assertLess(source.index('this._asyncTasks[task.group].push(task)'),source.index('task.state = startTask'))
        refresh = source.split('public function cppRefreshTask')[1].split('public function update')[0]
        self.assertIn('task.result = res;',refresh)
        self.assertNotIn('task.state =',refresh)
