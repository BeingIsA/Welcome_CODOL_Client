"""拆分后的事件契约：改名提交顺序、失败补偿和准备阶段收尾。"""
from pathlib import Path
from copy import deepcopy
import json
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from codol_rebuild.adapters.game.v1649370.session_bridge import GameSessionBridge
from codol_rebuild.adapters.game.v1649370 import player_name
from codol_rebuild.adapters.game.v1649370 import layout, profile
from codol_rebuild.adapters.game.v1649370.profile_bridge import LocalProfileBridge
from codol_rebuild.bootstrap import launch
from codol_rebuild.runtime.scheduler import PeriodicTask
from codol_rebuild.runtime.session import ProcessSession

class BridgeEventTests(unittest.TestCase):
    def bridge(self, fail=False):
        events=[]; memory={};ctx=bytearray(716)
        struct.pack_into('<I',ctx,180,0x30000000);struct.pack_into('<I',ctx,196,0x30000000)
        def write(address,data):
            events.append(('write',address,bytes(data)));memory[address]=bytes(data)
        process=SimpleNamespace(main_thread_id=17,read=lambda address,size:memory.get(address,bytes(size)),
            write=write,context=lambda tid:(1,ctx),set_context=lambda *a:None,close_thread=lambda h:events.append(('close',)),
            periodic=lambda interval:PeriodicTask(interval))
        bridge=GameSessionBridge(SimpleNamespace(local_profile=True),Path('.'),None,None,{},lambda r:None,lambda n:None,process,None,(OSError,ValueError))
        class Names:
            name_snapshot={'revision':2,'value':'Before'}
            def validate_name(self,value):events.append(('validate',value))
            def rename(self,value):
                events.append(('commit',value))
                if fail:raise ValueError('conflict')
                self.name_snapshot={'revision':3,'value':value}
        bridge.profiles=Names()
        bridge.observe_state=lambda:{'dw_enabled':0}
        bridge.name_adapter={'state':0x20000000,'ready':0x20001000,'traps':{101:'request',102:'applied',103:'rolled_back'}}
        return bridge,events

    def test_success_requires_readback_then_commit_then_ack(self):
        bridge,events=self.bridge()
        with patch.object(player_name,'read_request',return_value='After'),patch.object(player_name,'stage_name',side_effect=lambda *a:events.append(('stage',))),patch.object(player_name,'verify_applied',side_effect=lambda *a,**k:events.append(('readback',))):
            bridge.handle_exception(SimpleNamespace(tid=17),0x80000003,101,{})
            bridge.handle_exception(SimpleNamespace(tid=17),0x80000003,102,{})
        self.assertLess(events.index(('stage',)),events.index(('readback',)))
        self.assertLess(events.index(('readback',)),events.index(('commit','After')))
        self.assertLess(events.index(('commit','After')),events.index(('write',0x20000004,b'\1\0\0\0')))
        self.assertIsNone(bridge.pending_name)

    def test_failed_commit_requests_rollback_and_preserves_snapshot(self):
        bridge,events=self.bridge(fail=True);bridge.pending_name='After'
        with patch.object(player_name,'verify_applied') as verify:
            row={};bridge.handle_exception(SimpleNamespace(tid=17),0x80000003,102,row)
            self.assertEqual(row['local_player_name_error'],'conflict')
            self.assertIn(('write',0x20000004,bytes(4)),events)
            self.assertEqual(bridge.profiles.name_snapshot,{'revision':2,'value':'Before'})
            bridge.handle_exception(SimpleNamespace(tid=17),0x80000003,103,{})
            self.assertTrue(verify.call_args.kwargs['rollback'])
        self.assertIsNone(bridge.pending_name)

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
            bridge.complete_native(self.data, self.pool, True)

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
                    self.assertTrue(events[0][2]['local_store']['purchases_supported'])
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
            with patch('codol_rebuild.adapters.game.v1649370.purchase.quote', return_value={'sku': 1}) as quote:
                self.assertEqual(bridge.quote(0x24000000), {'sku': 1})
                self.assertEqual(quote.call_args.args[2][545]['type_bytes'][0], 0)

class LifecycleTests(unittest.TestCase):
    def test_prepare_failure_records_result_without_starting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(launch,'ROOT',Path(tmp)),patch.object(launch,'prepare',side_effect=ValueError('bad image')),patch.object(launch,'ProcessSession') as cls:
            process=cls.return_value;process.is_running.return_value=False
            result=launch.run(SimpleNamespace(name='failure'))
            self.assertEqual(result['error'],'bad image')
            process.start.assert_not_called();process.close.assert_called_once()
            self.assertTrue((Path(tmp)/'workspaces/offline-runs/failure/result.json').is_file())

    def test_prepare_only_never_starts_guard_or_debug_loop(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(launch,'ROOT',Path(tmp)),patch.object(launch,'prepare',return_value={}),patch.object(launch,'ProcessSession') as cls:
            result=launch.run(SimpleNamespace(name='prepared',prepare_only=True))
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
