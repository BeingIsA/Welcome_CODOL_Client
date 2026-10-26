"""只测试队列计划；不访问进程、不启动游戏。"""
import copy
import struct
import unittest

from codol_rebuild.adapters.game.v1649370.frontend.menu_queue import build_queue_plan, EXPECTED_POINTER, EXPECTED_CAPACITY, COMMAND


class MenuQueueTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(thread_id=42, main_thread_id=42, return_address=0x4fe615,
                          local_client_index=0, controller_index=0, engine_returned=True,
                          dw_enabled=0, ui_resource_count=3, client_object_ready=True,
                          already_queued=False)

    def plan(self, data=b'', **changes):
        state = self.state | changes
        return build_queue_plan(EXPECTED_POINTER, EXPECTED_CAPACITY, len(data), data, state)

    def test_append_preserves_existing_command_and_payload_terminator(self):
        existing=b'echo pending';before=copy.deepcopy(self.state)
        plan=self.plan(existing)
        self.assertEqual(plan['payload_address'],EXPECTED_POINTER+len(existing))
        self.assertEqual(existing+plan['payload'],b'echo pending\nswitchtomainmenu\n\0')
        self.assertEqual(struct.unpack('<I',plan['length_bytes'])[0],len(existing)+len(COMMAND))
        self.assertEqual(plan['new_length'],len(existing)+len(COMMAND))
        self.assertEqual(self.state,before)

    def test_exact_capacity_including_nul_and_one_byte_overflow(self):
        available=EXPECTED_CAPACITY-len(COMMAND)-1
        self.assertEqual(self.plan(b'x'*available)['new_length'],EXPECTED_CAPACITY-1)
        with self.assertRaises(ValueError):self.plan(b'x'*(available+1))

    def test_early_other_thread_wrong_caller_and_duplicate_rejected(self):
        cases = [dict(engine_returned=False),dict(thread_id=43),dict(thread_id=0,main_thread_id=0),
                 dict(return_address=0x4fdaad),dict(local_client_index=1),dict(controller_index=1),
                 dict(dw_enabled=1),dict(ui_resource_count=0),dict(client_object_ready=False),
                 dict(already_queued=True)]
        for change in cases:
            with self.subTest(change=change),self.assertRaises(ValueError):self.plan(**change)
        self.assertEqual(self.plan(return_address=0x4fe672)['return_address'],0x4fe672)

    def test_pointer_length_snapshot_mismatch_and_bool_not_integer(self):
        cases = [(0,EXPECTED_CAPACITY,0,b''),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY+1,0,b''),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY,-1,b''),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY,EXPECTED_CAPACITY,b''),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY,2,b'x'),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY,False,b''),
                 (EXPECTED_POINTER,EXPECTED_CAPACITY,0,bytearray())]
        for args in cases:
            with self.subTest(args=args),self.assertRaises(ValueError):build_queue_plan(*args,self.state)

    def test_missing_or_coerced_state_cannot_approve_write(self):
        for field in self.state:
            state=self.state.copy();del state[field]
            with self.subTest(field=field),self.assertRaises(ValueError):
                build_queue_plan(EXPECTED_POINTER,EXPECTED_CAPACITY,0,b'',state)
        for change in (dict(engine_returned=1),dict(client_object_ready=1),dict(dw_enabled=False)):
            with self.subTest(change=change),self.assertRaises(ValueError):self.plan(**change)


if __name__=='__main__':unittest.main()
