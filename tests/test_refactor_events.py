"""拆分后的事件契约：改名提交顺序、失败补偿和准备阶段收尾。"""
from pathlib import Path
from copy import deepcopy
import json
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from codol_rebuild.adapters.game.v1649370.session.session_bridge import GameSessionBridge
from codol_rebuild.adapters.game.v1649370.accounts import player_name
from codol_rebuild.adapters.game.v1649370 import layout
from codol_rebuild.adapters.game.v1649370.accounts import profile
from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge
from codol_rebuild.bootstrap import launch
from codol_rebuild.runtime.scheduler import PeriodicTask
from codol_rebuild.runtime.session import ProcessSession

class BridgeEventTests(unittest.TestCase):
    def bridge(self):
        events=[]; memory={};ctx=bytearray(716)
        struct.pack_into('<I',ctx,180,0x30000000);struct.pack_into('<I',ctx,196,0x30000000)
        def write(address,data):
            events.append(('write',address,bytes(data)));memory[address]=bytes(data)
        process=SimpleNamespace(main_thread_id=17,read=lambda address,size:memory.get(address,bytes(size)),
            write=write,context=lambda tid:(1,ctx),set_context=lambda *a:None,close_thread=lambda h:events.append(('close',)),
            periodic=lambda interval:PeriodicTask(interval))
        bridge=GameSessionBridge(SimpleNamespace(local_profile=True),Path('.'),None,None,{},lambda r:None,lambda n:None,process,None,(OSError,ValueError))
        bridge.profiles = Mock()
        bridge.profiles.name_snapshot = {'revision': 2, 'value': 'Before'}
        bridge.observe_state = lambda: {'dw_enabled': 0}
        bridge.name_adapter = {'state': 0x20000000, 'ready': 0x20001000, 'traps': {101: 'restored'}}
        return bridge, events

    def test_offline_restore_reads_back_saved_name_without_issuing_rename(self):
        bridge, events = self.bridge()
        row = {}
        with patch.object(player_name, 'verify_applied') as verify:
            bridge.handle_exception(SimpleNamespace(tid=17), 0x80000003, 101, row)
        verify.assert_called_once_with(bridge.process.read, 0x20000000)
        self.assertEqual(events, [('write', 0x20000000, bytes(8)), ('close',)])
        self.assertEqual(row['local_player_name_restored']['value'], 'Before')
        self.assertEqual(bridge.profiles.method_calls, [])

    def test_installed_offline_entry_has_only_restore_trap_and_connection_message(self):
        from test_refactor_equivalence import MemoryProcess
        bridge, events = self.bridge()
        bridge.process = MemoryProcess()
        bridge._install_player_name_adapter(0x24000000, 0x25000000)
        adapter = bridge.name_adapter
        self.assertIsNone(bridge.server_name_bridge)
        self.assertNotIn('poll', adapter)
        self.assertEqual(list(adapter['traps'].values()), ['restored'])
        message = '改名需要联网，请连接服务器后重试。'.encode('utf-8') + bytes(1)
        self.assertEqual(bridge.process.read(adapter['state'] + 144, len(message)), message)

    def test_duplicate_request_waits_for_first_server_result(self):
        bridge, events = self.bridge()
        session = Mock()
        name_bridge = player_name.ServerNameBridge(
            bridge.process, bridge.profiles, session, bridge.name_adapter, (ValueError,), Mock())
        with patch.object(player_name, 'read_request', return_value='After'), patch.object(player_name, 'encode_name'):
            name_bridge.handle('request', 0, {})
            name_bridge.handle('request', 0, {})
        session.begin.assert_called_once_with('After')
        self.assertTrue(name_bridge.waiting_ui)
        self.assertEqual(events, [])
        confirmed_inventory = {'revision': 2, 'items': []}
        session.poll.return_value = SimpleNamespace(error=None, notify=True, display_name='After',
                                                    inventory=confirmed_inventory)
        with patch.object(player_name, 'stage_name') as stage:
            name_bridge.handle('poll', 0, {})
        self.assertFalse(name_bridge.waiting_ui)
        bridge.profiles.apply_inventory.assert_called_once_with(confirmed_inventory)
        stage.assert_called_once_with(bridge.process.read, bridge.process.write, 0x20000000, 'After')
        self.assertIn(('write', 0x20000000 + 136, struct.pack('<II', 1, 1)), events)

    def test_first_online_profile_confirmation_controls_lobby_activation(self):
        bridge, events = self.bridge()
        bridge.online_session = Mock()
        bridge.preview_traps = {400: 'before'}
        bridge.preview_finish_address = 480
        bridge.profiles.initial_profile_ready = False
        event = SimpleNamespace(tid=17)
        for frame in range(2):
            self.assertTrue(bridge.handle_exception(event, 0x80000003, 400, {}))
            context = bridge.process.context(event.tid)[1]
            self.assertEqual(struct.unpack_from('<I', context, 184)[0], 480)
        self.assertEqual(bridge.preview_cycles.count, 0)

        bridge.profiles.initial_profile_ready = True
        bridge.observe_state = lambda: {'dw_enabled': 0, 'client_object_ready': True}
        for address, value in ((0x30000000+28, 1), (0x30000000+36, bridge.preview_caller_return),
                               (layout.CATALOG_COUNT, 3), (layout.CATALOG_RECORDS, 0x10000)):
            bridge.process.write(address, struct.pack('<I', value))
        bridge.process.write(layout.CATALOG_READY, bytes([1]))
        with patch('codol_rebuild.adapters.game.v1649370.frontend.viewer.view_state',
                   return_value={'viewer_initialized': 1, 'weapon_definition_count': 2}):
            self.assertTrue(bridge.handle_exception(event, 0x80000003, 400, {}))
        self.assertEqual(bridge.preview_cycles.count, 1)
        self.assertEqual(struct.unpack_from('<I', context, 184)[0], 401)

    def test_online_idle_never_reads_running_game_between_native_frames(self):
        bridge, events = self.bridge()
        bridge.online_session = Mock()
        bridge.idle()
        bridge.profiles.save.assert_not_called()

    def test_online_rename_unblocks_only_when_confirmed_name_is_restored(self):
        bridge, events = self.bridge()
        bridge.rename_session = Mock()
        calls = Mock()
        calls.attach_mock(bridge.rename_session.complete, 'enable_rename')
        with patch.object(player_name, 'verify_applied') as verify:
            calls.attach_mock(verify, 'readback')
            bridge.handle_exception(SimpleNamespace(tid=17), 0x80000003, 101, {})
        self.assertEqual([entry[0] for entry in calls.mock_calls], ['readback', 'enable_rename'])

    def test_wrong_thread_rejected_before_native_access(self):
        bridge,events=self.bridge()
        with self.assertRaises(ValueError):bridge.handle_exception(SimpleNamespace(tid=99),0x80000003,101,{})
        self.assertEqual(events,[])

    def test_profile_events_delegate_without_mutating_bridge_state(self):
        bridge, events = self.bridge()
        bridge.profiles = SimpleNamespace(prepare_native=Mock(), complete_native=Mock())
        bridge.profile_traps = {201: (0x21000000, 'prepare', 0x22000000),
                                202: (0x21000000, 'after', 0x22000000)}
        bridge.handle_exception(SimpleNamespace(tid=17), 0x80000003, 201, {})
        bridge.profiles.prepare_native.assert_called_once_with(
            0x21000000, 0x22000000, Path('local-profile'), 0x20000000)
        bridge.handle_exception(SimpleNamespace(tid=17), 0x80000003, 202, {})
        bridge.profiles.complete_native.assert_called_once_with(0x21000000, 0x22000000, False)
        self.assertEqual(events, [('close',), ('close',)])


class ProfileLifecycleTests(unittest.TestCase):
    """独立内存模型验证原生发布顺序、就绪边界和对外快照隔离。"""
    data, pool, name_state = 0x21000000, 0x22000000, 0x23000000

    def prepare_bridge(self, folder, *, initialized=True, named=True):
        memory, writes, events = {}, [], []
        def put(address, value):
            memory.update({address+i: byte for i, byte in enumerate(value)})
        def read(address, size):
            return bytes(memory.get(address+i, 0) for i in range(size))
        def write(address, value):
            writes.append((address, bytes(value)))
            put(address, value)
        process = SimpleNamespace(read=read, write=write, periodic=lambda interval: PeriodicTask(interval))
        schemas = {}
        for label in ('playerdata', 'playersecuredata'):
            path = folder / (label + '.json')
            path.write_text(json.dumps({'label': label}), encoding='utf-8')
            schemas[label] = {'schema': str(path)}
        captured = {'schemas': schemas, 'tables': {'mp/coWeaponPrimaryRankTable.csv': [['0', '99']]},
                    'catalog': [{'id': 545, 'type_bytes': [0, 2, 0, 0]}]}
        account = {'profiles': {'public': b'public', 'secure': b'secure'}, 'revision': 1,
                   'initialization': {'version': 1, 'owned_ids': [545], 'gp': 2000, 'cp': 100} if initialized else None}
        service = SimpleNamespace(account=account, name_snapshot={'revision': 2, 'value': '玩家' if named else None},
                                  codec=SimpleNamespace(identities={'public': {'version': 9}}))
        def initialize():
            events.append('initialize')
            return account
        service.initialize = initialize
        bridge = LocalProfileBridge(process, lambda *args: service, {},
                                    lambda row: events.append(('record', bridge.ready, deepcopy(row))))
        def stage_name(read, write, state, value):
            self.assertEqual(value, '玩家')
            events.append('restore_name')
            write(state, b'NAME')
        def populate(read, write, pool, catalog, weapon_xp, *, owned_ids, quantities):
            events.append('populate')
            self.assertEqual(read(profile.PUBLIC+144, 6), b'public')
            self.assertEqual(read(profile.SECURE+144, 6), b'secure')
            self.assertEqual((catalog, weapon_xp), (captured['catalog'], 99))
            self.assertEqual(owned_ids, [545] if initialized else None)
            self.assertIsNone(quantities)
            write(pool, b'POOL')
            return {'items': 1, 'capacity': profile.CAPACITY, 'pool': hex(pool), 'weapon_experience': 99}
        with patch.object(profile, 'capture', return_value=captured) as capture, \
                patch.object(profile, 'populate_inventory', side_effect=populate), \
                patch.object(player_name, 'stage_name', side_effect=stage_name):
            bridge.prepare_native(self.data, self.pool, folder, self.name_state)
        capture.assert_called_once_with(process.read, self.data, folder, self.pool)
        expected = [(self.name_state, b'NAME')] if named else []
        expected += [(profile.PUBLIC+144, b'public'), (profile.SECURE+144, b'secure'),
                     (self.pool, b'POOL'), (self.data+64, struct.pack('<I', 1))]
        self.assertEqual(writes, expected)
        self.assertEqual(events, ['initialize', *(['restore_name'] if named else []), 'populate'])
        self.assertFalse(bridge.ready)
        # 模拟原生构造与缓存回调完成；不借由桥的内部属性设置就绪或库存。
        put(self.pool-16, b'LOCAL-INVENTORY!')
        put(self.pool+profile.CAPACITY*profile.STRIDE, b'LOCAL-INVENTORY!')
        put(layout.INVENTORY_COUNT, struct.pack('<I', 1))
        put(0x82C4A86, b'\x01')
        put(0x82C4AA4, struct.pack('<I', 1))
        put(self.data+68, struct.pack('<I', 545))
        writes.clear(); events.clear()
        return bridge, process, writes, events, put, service

    @staticmethod
    def selected_character(blob, path):
        return {'currentCharacterIndex': 0, 'characterClasses.0.slots.0': 545}[path]

    def complete(self, bridge):
        with patch('codol_rebuild.adapters.game.profile_format.fields.Fields') as fields:
            fields.return_value.read.side_effect = self.selected_character
            bridge.complete_native(self.data, self.pool, False)

    def test_prepare_and_complete_preserve_publication_order(self):
        for initialized in (False, True):
            for named in (False, True):
                with self.subTest(initialized=initialized, named=named), tempfile.TemporaryDirectory() as tmp:
                    bridge, process, writes, events, _, _ = self.prepare_bridge(
                        Path(tmp), initialized=initialized, named=named)
                    self.complete(bridge)
                    addresses = [0x82C4AEC, 0x82C4A88, 0x82C4AA4]
                    if initialized:
                        addresses += [0xC8E4078, 0xC8E407C, 0xC8E4098, 0xC8E409C]
                        self.assertEqual(profile.currency_snapshot(process.read), {'gp': 2000, 'cp': 100})
                    self.assertEqual([address for address, _ in writes], addresses+[self.data+72])
                    self.assertEqual(writes[-1][1], struct.pack('<I', 1))
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0][:2], ('record', False))
                    self.assertFalse(events[0][2]['local_store']['purchases_supported'])
                    self.assertTrue(bridge.ready)
                    self.assertEqual(bridge.inventory_count, 1)
                    self.assertEqual(bridge.inventory['selected_character_cache'], 545)

    def test_native_validation_failure_never_publishes_ready(self):
        for broken in ('boundary', 'count', 'character'):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as tmp:
                bridge, process, writes, events, put, _ = self.prepare_bridge(Path(tmp))
                if broken == 'boundary':
                    put(self.pool-16, bytes(16))
                elif broken == 'count':
                    put(layout.INVENTORY_COUNT, struct.pack('<I', 2))
                else:
                    put(self.data+68, struct.pack('<I', 0))
                with self.assertRaises(ValueError):
                    self.complete(bridge)
                self.assertFalse(bridge.ready)
                self.assertEqual(process.read(self.data+72, 4), bytes(4))
                self.assertEqual((writes, events), ([], []))

    def test_diagnostic_properties_are_detached_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _, _, _, _, service = self.prepare_bridge(Path(tmp))
            account = bridge.account
            account['initialization']['owned_ids'].append(999)
            inventory = bridge.inventory; inventory['items'] = 999
            catalog = bridge.catalog; catalog[545]['type_bytes'][0] = 999
            schemas = bridge.schemas; schemas['public']['label'] = 'changed'
            name = bridge.name_snapshot; name['value'] = 'changed'
            identities = bridge.profile_identities; identities['public']['version'] = 999
            self.assertEqual(bridge.account['initialization']['owned_ids'], [545])
            self.assertEqual(bridge.inventory_count, 1)
            self.assertEqual(bridge.catalog[545]['type_bytes'][0], 0)
            self.assertEqual(bridge.schemas['public']['label'], 'playerdata')
            self.assertEqual(bridge.name_snapshot['value'], '玩家')
            self.assertEqual(service.codec.identities['public']['version'], 9)
            for attribute in ('ready', 'inventory_count', 'account', 'inventory', 'catalog',
                              'schemas', 'name_snapshot', 'profile_identities'):
                with self.assertRaises(AttributeError):
                    setattr(bridge, attribute, None)

class LifecycleTests(unittest.TestCase):
    def test_runtime_lease_released_after_failed_process_start(self):
        """程序启动失败也须先关闭进程，再释放共享运行槽位。"""
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(launch, 'ROOT', Path(directory)), \
             patch.object(launch, 'prepare') as prepare, \
             patch.object(launch, 'ProcessSession') as process_class, \
             patch.object(launch, 'GameSessionBridge'), \
             patch.object(launch, 'DebugLoop'):
            lease = Mock(path=Path(directory) / 'program')
            prepare.return_value = {'runtime_lease': lease, 'image': None, 'pe': None,
                                    'exe': lease.path / 'game.exe', 'settings': None,
                                    'players_directory': Path(directory) / 'players'}
            process = process_class.return_value
            process.is_running.return_value = False
            process.start.side_effect = ValueError('start failed')
            calls = Mock()
            calls.attach_mock(process.close, 'close_process')
            calls.attach_mock(lease.close, 'release_directory')
            result = launch.run(SimpleNamespace(name='lease-failure', server_address=None))
            self.assertEqual(result['error'], 'start failed')
            self.assertEqual([entry[0] for entry in calls.mock_calls], ['close_process', 'release_directory'])

    def test_prepare_failure_records_result_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launch,'ROOT',Path(tmp)),patch.object(launch,'prepare',side_effect=ValueError('bad image')),patch.object(launch,'ProcessSession') as cls:
            process=cls.return_value;process.is_running.return_value=False
            result=launch.run(SimpleNamespace(name='failure', server_address=None))
            self.assertEqual(result['error'],'bad image')
            process.start.assert_not_called();process.close.assert_called_once()
            self.assertTrue((Path(tmp)/'workspaces/offline-runs/failure/result.json').is_file())

    def test_prepare_only_never_starts_guard_or_debug_loop(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'ROOT',Path(tmp)),patch.object(launch,'prepare',return_value={}),patch.object(launch,'ProcessSession') as cls:
            result=launch.run(SimpleNamespace(name='prepared',prepare_only=True, server_address=None))
            self.assertTrue(result['prepared_only']);cls.return_value.start.assert_not_called()
            cls.return_value.close.assert_called_once()

    def test_closed_process_cannot_read_or_write(self):
        process=ProcessSession(None,None,None,{},lambda r:None,lambda n:None,None)
        process.closed=True
        with self.assertRaises(ValueError):process.read(0x400000,4)
        with self.assertRaises(ValueError):process.write(0x400000,b'1234')

    def test_periodic_due_and_forced_checkpoint(self):
        now=[0.0];task=PeriodicTask(2,clock=lambda:now[0])
        self.assertTrue(task.due());self.assertFalse(task.due())
        now[0]=1;self.assertTrue(task.due(force=True))
        now[0]=2;self.assertFalse(task.due())
        now[0]=3;self.assertTrue(task.due())
