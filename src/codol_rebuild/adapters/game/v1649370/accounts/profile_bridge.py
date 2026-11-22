"""本地档案原生桥：拥有初始化、发布、就绪状态、一致采样与应用服务连接。"""
from copy import deepcopy
import json
from pathlib import Path
import struct
import time

from codol_rebuild.adapters.game.v1649370 import layout
from codol_rebuild.adapters.game.v1649370.accounts.profile import PUBLIC, SECURE, CAPACITY, STRIDE
from codol_rebuild.adapters.game.v1649370.frontend.viewer import view_state


class LocalProfileBridge:
    """集中维护原生档案生命周期，对外只提供操作和独立的诊断快照。"""
    def __init__(self, process, factory, log, record, inventory_session=None):
        self.process=process; self.factory=factory; self.log=log; self.record=record
        self._service=None; self._schemas=None; self._ready=False; self._inventory=None
        self._catalog={}
        self._online_inventory = inventory_session
        self._confirmed_inventory = None
        self._task=process.periodic(2); self._last_view=None

    @property
    def _account(self):
        return self._service.account if self._service else None

    @property
    def ready(self):
        """返回原生档案是否已完成发布；就绪状态只能由本桥推进。"""
        return self._ready

    @property
    def initial_profile_ready(self):
        """初始化档案已在服务端确认时，允许原生大厅继续激活。"""
        return self._ready and self._service.initial_profile_ready

    @property
    def inventory_count(self):
        """返回已发布库存数量，供原生任务完成日志使用。"""
        return self._inventory['items'] if self._inventory is not None else None

    @property
    def account(self):
        """返回独立的账号诊断快照，不允许调用方修改当前账号。"""
        return deepcopy(self._account)

    @property
    def schemas(self):
        """返回独立的格式诊断快照。"""
        return deepcopy(self._schemas)

    @property
    def inventory(self):
        """返回独立的原生库存诊断快照。"""
        return deepcopy(self._inventory)

    @property
    def catalog(self):
        """返回独立的物品目录快照，供专项验收筛选物品。"""
        return deepcopy(self._catalog)

    @property
    def name_snapshot(self):
        """返回独立的昵称快照，避免原生任务持有应用层可变状态。"""
        return deepcopy(self._service.name_snapshot) if self._service else None

    @property
    def profile_identities(self):
        """返回档案格式身份快照，供专项验收读取保存结果。"""
        return deepcopy(self._service.codec.identities) if self._service else None

    def prepare_native(self, profile_data, pool, capture_dir, name_state):
        """在准备暂停点捕获并初始化账号，按原顺序恢复昵称、档案和库存。"""
        from codol_rebuild.adapters.game.v1649370.accounts.profile import capture
        from codol_rebuild.adapters.game.v1649370.accounts.profile import populate_inventory
        captured = capture(self.process.read, profile_data, capture_dir, pool)
        schemas = {kind: json.loads(Path(captured['schemas'][label]['schema']).read_text(encoding='utf-8')) for kind, label in [('public', 'playerdata'), ('secure', 'playersecuredata')]}
        account = self._initialize(schemas, captured['tables'], captured['catalog'])
        from codol_rebuild.adapters.game.v1649370.accounts import player_name
        if self._service.name_snapshot['value'] is not None:
            player_name.stage_name(self.process.read, self.process.write, name_state, self._service.name_snapshot['value'])
        for kind, base in [('public', PUBLIC), ('secure', SECURE)]:
            blob = account['profiles'][kind]
            self.process.write(base + 144, blob)
            if self.process.read(base + 144, len(blob)) != blob:
                raise ValueError('Local profile readback mismatch')
        self.log['local_profile'] = {k: v for k, v in account.items() if k not in ('profiles', 'initialization')}
        weapon_xp = max((int(r[1]) for r in captured['tables']['mp/coWeaponPrimaryRankTable.csv'] if r[0].isdigit()))
        state = account.get('initialization')
        if self._online_inventory is not None:
            snapshot = self._online_inventory.initial_snapshot
            if snapshot is None:
                raise ValueError('尚未取得服务器仓库')
            state = {'owned_ids': [item['itemId'] for item in snapshot['items']],
                     'quantities': {str(item['itemId']): item['quantity'] for item in snapshot['items']}}
            self._confirmed_inventory = snapshot
        self._inventory = populate_inventory(self.process.read, self.process.write, pool, captured['catalog'], weapon_xp,
                                             owned_ids=state['owned_ids'] if state else None,
                                             quantities=state.get('quantities') if state else None)
        if self._online_inventory is not None:
            self.log['online_inventory_revision'] = self._confirmed_inventory['revision']
        elif state:
            self.log['local_account_initialization'] = {'version': state['version'], 'owned_items': len(state['owned_ids']),
                                                        'gp': state['gp'], 'cp': state['cp']}
        self.log['local_inventory']=self._inventory.copy()
        self.process.write(profile_data + 64, struct.pack('<I', self._inventory['items']))
        self.log['local_profile_capture'] = {'catalog_count': len(captured['catalog']), 'tables': {k: len(v) for k, v in captured['tables'].items()}}

    def complete_native(self, profile_data, pool, purchases_supported):
        """核验原生缓存后发布商城和余额，最后推进原生及宿主就绪状态。"""
        if self.process.read(pool - 16, 16) != b'LOCAL-INVENTORY!' or self.process.read(pool + CAPACITY * STRIDE, 16) != b'LOCAL-INVENTORY!':
            raise ValueError('Local inventory boundary changed')
        native_count = struct.unpack('<I', self.process.read(layout.INVENTORY_COUNT, 4))[0]
        if native_count != self._inventory['items']:
            raise ValueError('Native inventory count mismatch')
        self._inventory['native_count'] = native_count
        self._inventory['canaries_intact'] = True
        from codol_rebuild.adapters.game.profile_format.fields import Fields
        fields = Fields(self._schemas['public'])
        selected = fields.read(self._account['profiles']['public'], 'currentCharacterIndex')
        expected_character = fields.read(self._account['profiles']['public'], f'characterClasses.{selected}.slots.0')
        cached_character = struct.unpack('<I', self.process.read(profile_data + 68, 4))[0]
        self._inventory['selected_character_cache'] = cached_character
        if cached_character != expected_character or cached_character <= 0:
            raise ValueError('Native selected-character cache not initialized for inventory UI')
        from codol_rebuild.adapters.game.v1649370.accounts.profile import publish_empty_local_store
        self.log['local_store'] = publish_empty_local_store(self.process.read, self.process.write, native_count)
        self.log['local_store']['purchases_supported'] = purchases_supported
        state = self._confirmed_inventory if self._online_inventory is not None else self._account.get('initialization')
        if state:
            from codol_rebuild.adapters.game.v1649370.accounts.profile import publish_currency
            self.log['local_currency'] = publish_currency(self.process.read, self.process.write, state)
        self.process.write(profile_data + 72, struct.pack('<I', 1))
        if self.process.read(profile_data + 72, 4) != struct.pack('<I', 1):
            raise ValueError('Local UI stats readiness readback mismatch')
        self.log['local_profile_ui_stats_ready'] = True
        self.record({'local_store': self.log['local_store']})
        self.log['local_inventory']=self._inventory.copy()
        self._ready = True
        self.log['local_profile_native_cache_returned'] = True

    def apply_inventory(self, snapshot):
        """服务端确认快照只沿原生库存发布；迟到的回包不能覆盖较新版本。"""
        if not self._ready or self._confirmed_inventory is None:
            raise ValueError('联机仓库尚未完成初始发布')
        if snapshot['revision'] <= self._confirmed_inventory['revision']:
            return
        from codol_rebuild.adapters.game.v1649370.accounts.profile import publish_inventory_snapshot
        publish_inventory_snapshot(self.process.read, self.process.write, self._inventory, self._catalog, snapshot)
        self._confirmed_inventory = snapshot
        self.log['online_inventory_revision'] = snapshot['revision']

    def _initialize(self, schemas, tables, catalog):
        """保存本桥输入并创建应用会话；原生状态不暴露给会话编排器。"""
        self._schemas=schemas
        self._catalog={item['id']: item for item in catalog}
        self._service=self.factory(schemas,tables,catalog)
        return self._service.initialize()

    def validate_name(self, value):
        return self._service.validate_name(value)

    def check_name_card(self):
        """用已发布的服务端仓库核对改名卡，扣卡由服务端事务执行。"""
        from codol_rebuild.adapters.game.v1649370.accounts.player_name import NAME_CARD
        if self._confirmed_inventory is None:
            raise ValueError('联机仓库尚未就绪')
        if not any(item['itemId'] == NAME_CARD for item in self._confirmed_inventory['items']):
            raise ValueError('仓库中没有改名卡')

    def synchronize_name(self, value):
        """把服务端已确认的改名结果写入当前会话。"""
        return self._service.synchronize_name(value)

    def _sample_plaintext_profiles(self):
        """在既有保存入口取得前后读取一致的快照，仅在宿主副本解除公共档案编码。"""
        from codol_rebuild.adapters.game.v1649370.accounts.profile_codec import public_profile_plaintext
        profile_addresses = (('public', PUBLIC), ('secure', SECURE))
        representation_states = {
            kind: self.process.read(base + 0x11128, 5) for kind, base in profile_addresses}
        # 安全档案的编码字段集合尚未确认，继续拒绝编码态安全档案；
        # 两份档案仍作为同一事务保存，force 不绕过表示和稳定性检查。
        if representation_states['secure'][4]:
            return None
        profile_blobs = {
            kind: self.process.read(base + 0x90, layout.BUFFER_CAPACITY)
            for kind, base in profile_addresses}
        if any(self.process.read(base + 0x90, layout.BUFFER_CAPACITY) != profile_blobs[kind]
               for kind, base in profile_addresses):
            return None
        if any(self.process.read(base + 0x11128, 5) != representation_states[kind]
               for kind, base in profile_addresses):
            return None
        encoding_key, encoded = struct.unpack('<IB', representation_states['public'])
        # 直接保存编码值曾导致角色缓存无法初始化；但返回大厅仍可保持编码态，
        # 一律跳过又会丢失后续配装修改。因此只转换副本，不调用原生解码函数。
        profile_blobs['public'] = public_profile_plaintext(
            profile_blobs['public'], self._schemas['public'], encoding_key, encoded)
        return profile_blobs

    def save(self, force=False):
        if not self._account or not self._ready: return
        if not self._task.due(force=force): return
        mem=self.process.read
        blobs = self._sample_plaintext_profiles()
        if blobs is None: return
        current_view=view_state(mem)
        if current_view!=self._last_view:
            self._last_view=current_view
            self.log['local_view_state']=current_view
            self.record({'local_view_state':current_view})
        character=self._service.inspect_snapshot(blobs)
        pool=int(self._inventory['pool'],16)
        if mem(pool-16,16)!=b'LOCAL-INVENTORY!' or mem(pool+CAPACITY*STRIDE,16)!=b'LOCAL-INVENTORY!':
            raise ValueError('Inventory boundary changed')
        if self._service.save(blobs):
            row = {'character': character, 'time': time.time()}
            self.record({'account_profile_sampled': row})
