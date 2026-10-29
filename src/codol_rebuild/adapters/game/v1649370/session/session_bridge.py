"""1649370 会话桥：版本补丁安装、原生事件解释和 UI 完成时序。"""
import hashlib, struct
from codol_rebuild.runtime.contracts import ProcessAccess, DebugControl
from codol_rebuild.adapters.game.v1649370 import layout
from codol_rebuild.adapters.game.v1649370.frontend.menu_preview import PreviewActivationCycles
from codol_rebuild.adapters.game.v1649370.accounts.profile_bridge import LocalProfileBridge

class GameSessionBridge:
    """仅拥有版本状态；进程和账号实现由装配入口注入。"""
    def __init__(self, args, run_dir, image, pe, log, record, phase_done, process: ProcessAccess, account_factory, account_errors, multiplayer=None, rename_session=None, broadcast_session=None, realtime_dispatcher=None, chat_session=None, online_session=None, match_session=None):
        self.args=args; self.run_dir=run_dir; self.image=image; self.pe=pe
        self.log=log; self.record=record; self.phase_done=phase_done; self.process=process
        self.account_factory=account_factory; self.account_errors=account_errors
        self.preview_traps={}; self.preview_metadata={}; self.preview_cycles=PreviewActivationCycles()
        self.profile_traps={}; self.name_adapter=None; self.pending_name=None
        self.recharge_adapter=None
        self.purchase_adapter=None
        self.profiles=LocalProfileBridge(process, account_factory, log, record)
        self.debug=None
        self.schema_captured=False
        self.preview_caller_return = 0x5EBFF8
        self.session_traps = {}
        self.online_session = online_session
        self.online_bridge = None
        self.realtime_dispatcher = realtime_dispatcher
        self.multiplayer = None
        self.rename_session = rename_session
        self.server_name_bridge = None
        self.chat_bridge = None
        self.match_session = match_session
        self.match_bridge = None
        if match_session is not None and multiplayer is not None:
            from codol_rebuild.adapters.game.v1649370.matches.session_bridge import MatchBridge
            self.match_bridge = MatchBridge(process, match_session, multiplayer.confirmed_room_id,
                (args.game_server_ip, args.game_server_port), record)
        self.broadcast_bridge = None
        if broadcast_session is not None:
            from codol_rebuild.adapters.game.v1649370.chat.broadcast import BroadcastBridge
            self.broadcast_bridge = BroadcastBridge(
                process, broadcast_session, self.profiles.consume_item)
        if multiplayer is not None:
            from codol_rebuild.adapters.game.v1649370.rooms.room_session_bridge import RoomSessionBridge
            self.multiplayer = RoomSessionBridge(process, multiplayer, record)
            if rename_session is not None:
                self.multiplayer.account_ready = lambda: rename_session.ready

        if chat_session is not None and self.multiplayer is not None:
            from codol_rebuild.adapters.game.v1649370.chat.chat import ChatBridge
            self.chat_bridge = ChatBridge(process, chat_session, self.multiplayer.displayed_room_id)

    def _online_connected(self, player_id, display_name):
        """把已有登录结果显式交给需要身份的适配，不重复登录。"""
        if self.multiplayer is not None:
            self.multiplayer.connected(player_id)
        if self.match_session is not None:
            self.match_session.connected(player_id)
        if self.rename_session is not None:
            self.rename_session.connected(player_id, display_name)
        if self.broadcast_bridge is not None:
            self.broadcast_bridge.connected(player_id, display_name)
        if self.chat_bridge is not None:
            self.chat_bridge.connected(player_id, display_name)

    def checkpoint(self, force=False):
        self.profiles.save(force=force)

    def idle(self):
        if self.args.local_profile:
            try:self.checkpoint()
            except (OSError,ValueError) as error:self.log['local_profile_save_error']=str(error)

    def bind_debugger(self, debugger: DebugControl):
        self.debug=debugger

    def module_loaded(self, base):
        if base!=layout.IMAGE_BASE: raise ValueError('实际ImageBase不符')
        self.debug.arm(layout.CRT_ENTRY,'CRT entry'); self.debug.arm(layout.WIN_MAIN,'WinMain')
        if self.args.capture_schema:
            self.debug.arm(layout.FRONTEND_TICK,'Frontend tick'); self.debug.arm(layout.MAIN_MENU,'Switch to main menu')
        for value in self.args.breakpoint:
            address,label=value.split(':',1);self.debug.arm(int(address,0),label)

    def install_lobby_adapters(self):
        """在 CRT 入口暂停点安装大厅原生适配；此处仅编排依赖和安装顺序。"""
        from codol_rebuild.adapters.game.v1649370.frontend.menu_preview import validate_menu_preview_image
        validate_menu_preview_image(self.image.read_bytes())
        # 保持大厅代码页最先分配，避免改变后续原生地址布局。
        lobby_entry = self.process.allocate(4096)
        initializer_address = profile_data_address = room_plan = None
        if self.args.local_profile:
            self._install_local_ui_hooks()
            initializer_address, profile_data_address, inventory_pool_address, initialization_plan = self._install_profile_initialization()
            # profile_data + 72 是原生约定的档案就绪标志，各适配共享此地址。
            self._install_playlist_adapter(profile_data_address)
            self._install_room_name_adapter(profile_data_address)
            room_plan = self._install_room_adapter(profile_data_address)
            self._register_profile_initializer(initializer_address, profile_data_address, inventory_pool_address, initialization_plan)
            initializer_address = self._install_player_name_adapter(profile_data_address, initializer_address)
            self._install_recharge_adapter()
            self._install_purchase_adapter(profile_data_address)
        self._install_lobby_entry(lobby_entry, initializer_address, profile_data_address, room_plan)

    def _install_local_ui_hooks(self):
        """安装大厅资源加载观察点、局部 UI 补丁及角色查看事件。"""
        # 先验证全部补丁原字节，再依次写入，保留原有安装顺序。
        from codol_rebuild.adapters.game.v1649370.session import ui_movie
        if self.process.read(ui_movie.LOAD_SITE, len(ui_movie.LOAD_BYTES)) != ui_movie.LOAD_BYTES:
            raise ValueError('大厅 UI 加载调用边不匹配')
        self.debug.arm(ui_movie.LOAD_SITE, 'Local navigation resource')
        from codol_rebuild.adapters.game.v1649370.frontend.local_ui_services import local_optional_service_patches, local_welcome_page_patch
        welcome_patch = local_welcome_page_patch()
        local_patches = local_optional_service_patches() + [welcome_patch]
        for patch_address, original_bytes, replacement_bytes in local_patches:
            if self.process.read(patch_address, len(original_bytes)) != original_bytes:
                raise ValueError(f'Local callback mismatch at {patch_address:#x}')
        for patch_address, original_bytes, replacement_bytes in local_patches:
            self.process.write(patch_address, replacement_bytes)
            if self.process.read(patch_address, len(replacement_bytes)) != replacement_bytes:
                raise ValueError(f'Local callback readback mismatch at {patch_address:#x}')
        self.record({'local_unsupported_services': ['WarGod credit rating'], 'behavior': 'void request ignored; no score fabricated'})
        self.record({'local_welcome_auto_popup_disabled': True, 'callback': 'IsWelcomePageShowed', 'patch_va': hex(welcome_patch[0]), 'behavior': 'report welcome already shown; manual MOTD entry unchanged'})
        from codol_rebuild.adapters.game.v1649370.frontend.viewer import VIEW_TRACE_POINTS, VIEW_RESULT_POINTS
        for address, label in (VIEW_TRACE_POINTS | VIEW_RESULT_POINTS).items():
            self.debug.arm(address, label)

    def _install_profile_initialization(self):
        """分配档案和库存区域，安装初始化、就绪及角色查看适配。"""
        # 初始化页沿用原布局：主体占前 3072 字节，就绪适配占后续 256 字节，
        # 角色查看适配使用最后 768 字节；容量检查防止相互覆盖。
        from codol_rebuild.adapters.game.v1649370.accounts.profile import build_initializer, inventory_relocations, CAPACITY, STRIDE, INITIALIZER_DATA_SIZE
        from codol_rebuild.adapters.game.v1649370.accounts.profile import validate_account_native_contract
        validate_account_native_contract(self.process.read)
        from codol_rebuild.adapters.game.v1649370.accounts.profile import install_upload_checksum_order
        install_upload_checksum_order(self.process.read, self.process.write)
        initializer_address = self.process.allocate(4096)
        profile_data_address = self.process.allocate(INITIALIZER_DATA_SIZE)
        pool_allocation = self.process.allocate(CAPACITY * STRIDE + 32)
        inventory_pool_address = pool_allocation + 16
        self.process.write(pool_allocation, b'LOCAL-INVENTORY!')
        self.process.write(inventory_pool_address + CAPACITY * STRIDE, b'LOCAL-INVENTORY!')
        relocations = inventory_relocations(inventory_pool_address)
        for patch_address, original_bytes, replacement_bytes in relocations:
            if self.process.read(patch_address, len(original_bytes)) != original_bytes:
                raise ValueError(f'Inventory operand mismatch at {patch_address:#x}')
        for patch_address, original_bytes, replacement_bytes in relocations:
            self.process.write(patch_address, replacement_bytes)
        initialization_plan = build_initializer(initializer_address, profile_data_address, inventory_pool_address)
        self.process.write(initializer_address, initialization_plan['code'])
        self.process.write(profile_data_address, initialization_plan['data'])
        from codol_rebuild.adapters.game.v1649370.accounts.profile import build_stats_ready_adapter
        stats_entry = initializer_address + 3072
        stats = build_stats_ready_adapter(stats_entry, profile_data_address)
        if len(initialization_plan['code']) > 3072 or self.process.read(stats['patch_va'], 5) != stats['before']:
            raise ValueError('Local UI stats adapter image/region mismatch')
        self.process.write(stats_entry, stats['code'])
        self.process.write(stats['patch_va'], stats['after'])
        if self.process.read(stats_entry, len(stats['code'])) != stats['code'] or self.process.read(stats['patch_va'], 5) != stats['after']:
            raise ValueError('Local UI stats adapter readback mismatch')
        from codol_rebuild.adapters.game.v1649370.frontend.viewer import build_local_viewer_adapter
        viewer_entry = initializer_address + 3328
        viewer = build_local_viewer_adapter(viewer_entry, profile_data_address + 72)
        if len(stats['code']) > 256 or len(viewer['code']) > 768 or self.process.read(viewer['patch_va'], len(viewer['gate_before'])) != viewer['gate_before']:
            raise ValueError('Local viewer adapter image/region mismatch')
        self.process.write(viewer_entry, viewer['code'])
        self.process.write(viewer['patch_va'], viewer['after'])
        if self.process.read(viewer_entry, len(viewer['code'])) != viewer['code'] or self.process.read(viewer['patch_va'], 5) != viewer['after']:
            raise ValueError('Local viewer adapter readback mismatch')
        self.log['local_viewer_adapter'] = {'patch_va': hex(viewer['patch_va']), 'local_modes': viewer['local_modes'], 'body_va': hex(viewer['body_va']), 'exit_va': hex(viewer['exit_va'])}
        self.record({'local_viewer_adapter': self.log['local_viewer_adapter']})
        return initializer_address, profile_data_address, inventory_pool_address, initialization_plan

    def _install_playlist_adapter(self, profile_data_address):
        """安装玩法列表适配，使用档案数据区的就绪标志。"""
        from codol_rebuild.adapters.game.v1649370.frontend.playlist import build_local_playlist_adapter, validate_local_playlist_contract
        validate_local_playlist_contract(self.process.read)
        playlist_entry = self.process.allocate(4096)
        playlist = build_local_playlist_adapter(playlist_entry, profile_data_address + 72)
        if len(playlist['code']) > 4096:
            raise ValueError('Local playlist adapter exceeds allocation')
        self.process.write(playlist_entry, playlist['code'])
        if self.process.read(playlist_entry, len(playlist['code'])) != playlist['code']:
            raise ValueError('Local playlist adapter readback mismatch')
        self.process.executable(playlist_entry, 4096)
        for patch in playlist['patches']:
            self.process.write(patch['va'], patch['after'])
            if self.process.read(patch['va'], len(patch['after'])) != patch['after']:
                raise ValueError('Local playlist patch readback mismatch')
        self.log['local_playlist_adapter'] = {'entry': hex(playlist_entry), 'patches': [hex(patch['va']) for patch in playlist['patches']], 'policy': 'local ready offline, no season and zero configured playlist: UI ID -1; local -1 is invalid'}
        self.record({'local_playlist_adapter': self.log['local_playlist_adapter']})

    def _install_room_name_adapter(self, profile_data_address):
        """安装房间名适配，保留原生任务完成通知。"""
        from codol_rebuild.adapters.game.v1649370.rooms.room_name import build_room_name_adapter, validate_room_name_contract
        validate_room_name_contract(self.process.read)
        room_name_entry = self.process.allocate(4096)
        room_name = build_room_name_adapter(room_name_entry, profile_data_address + 72)
        self.process.write(room_name_entry, room_name['code'])
        if self.process.read(room_name_entry, len(room_name['code'])) != room_name['code']:
            raise ValueError('Room name adapter readback mismatch')
        self.process.executable(room_name_entry, 4096)
        self.process.write(room_name['patch_va'], room_name['after'])
        if self.process.read(room_name['patch_va'], 5) != room_name['after']:
            raise ValueError('Room name call edge readback mismatch')
        self.record({'local_room_name_adapter': {'entry': hex(room_name_entry), 'patch_va': hex(room_name['patch_va']), 'policy': 'local room names only; nonempty, <=255 UTF-8 bytes, no ASCII controls', 'completion': 'original cppRefreshTask; original UI task update'}})

    def _install_room_adapter(self, profile_data_address):
        """安装房间适配；联机模式注入身份地址及成员档案采集入口。"""
        from codol_rebuild.adapters.game.v1649370.rooms.room import build_room_adapter, validate_room_contract
        validate_room_contract(self.process.read)
        room_entry = self.process.allocate(4096)
        room_state = self.process.allocate(4096)
        if self.multiplayer:
            self.multiplayer.allocate()
            if self.match_bridge is not None:
                self.match_bridge.install(self.multiplayer.state+24)
        room_plan = build_room_adapter(room_entry, room_state, profile_data_address + 72,
                                  deferred=self.multiplayer is not None,
                                  identity_address=self.multiplayer.state+24 if self.multiplayer else None,
                                  profile_capture=self.multiplayer.profile_capture if self.multiplayer else None)
        if len(room_plan['code']) > 4096:
            raise ValueError('Local room adapter exceeds allocation')
        self.process.write(room_entry, room_plan['code'])
        if self.process.read(room_entry, len(room_plan['code'])) != room_plan['code']:
            raise ValueError('Local room adapter readback mismatch')
        self.process.executable(room_entry, 4096)
        for patch in room_plan['patches']:
            self.process.write(patch['va'], patch['after'])
            if self.process.read(patch['va'], len(patch['after'])) != patch['after']:
                raise ValueError('Local room patch readback mismatch')
        self.record({'local_room_adapter': {'entry': hex(room_entry), 'state': hex(room_state), 'patches': [hex(patch['va']) for patch in room_plan['patches']], 'completion': 'original native session/create callbacks; local room ownership only'}})
        return room_plan

    def _register_profile_initializer(self, initializer_address, profile_data_address, inventory_pool_address, initialization_plan):
        """启用档案初始化代码页，并登记准备与完成两个暂停点。"""
        self.process.executable(initializer_address, 4096)
        self.profile_traps[initialization_plan['trap']] = (profile_data_address, 'prepare', inventory_pool_address)
        self.profile_traps[initialization_plan['after']] = (profile_data_address, 'after', inventory_pool_address)
        self.record({'local_profile_bridge': hex(initializer_address), 'trap': hex(initialization_plan['trap'])})

    def _install_player_name_adapter(self, profile_data_address, initializer_address):
        """安装昵称适配，返回包裹档案初始化的昵称恢复入口。"""
        # 先恢复已保存昵称，再继续原档案初始化；大厅入口使用返回的新入口。
        # 联机改名另接主线程轮询，HTTP 仍由注入的应用会话执行。
        from codol_rebuild.adapters.game.v1649370.accounts import player_name
        player_name.validate_contract(self.process.read)
        name_entry = self.process.allocate(4096)
        name_state = self.process.allocate(4096)
        self.name_adapter = player_name.build_adapter(name_entry, name_state, profile_data_address + 72, initializer_address,
                                                     deferred=self.rename_session is not None)
        if self.rename_session is not None:
            self.process.write(name_state+136, bytes(520))
            self.server_name_bridge = player_name.ServerNameBridge(
                self.process, self.profiles, self.rename_session, self.name_adapter,
                self.account_errors, lambda message: self.multiplayer.notifications.message(message))
        if len(self.name_adapter['code']) > 4096:
            raise ValueError('昵称适配超出代码页')
        self.process.write(name_entry, self.name_adapter['code'])
        if self.process.read(name_entry, len(self.name_adapter['code'])) != self.name_adapter['code']:
            raise ValueError('昵称适配代码回读失败')
        self.process.executable(name_entry, 4096)
        self.process.write(self.name_adapter['patch_va'], self.name_adapter['after'])
        if self.process.read(self.name_adapter['patch_va'], 4) != self.name_adapter['after']:
            raise ValueError('昵称GFx回调安装回读失败')
        initializer_address = self.name_adapter['initializer']
        self.record({'local_player_name_adapter': {'entry': hex(name_entry), 'state': hex(name_state), 'policy': 'local offline only; consume name card atomically; durable commit before success; restore on startup'}})
        return initializer_address

    def _install_recharge_adapter(self):
        """安装充值专用 GFx 回调。"""
        from codol_rebuild.adapters.game.v1649370.accounts import ui_recharge
        ui_recharge.validate_contract(self.process.read)
        recharge_entry = self.process.allocate(4096)
        self.recharge_adapter = ui_recharge.build_adapter(recharge_entry)
        self.process.write(recharge_entry, self.recharge_adapter['code'])
        if self.process.read(recharge_entry, len(self.recharge_adapter['code'])) != self.recharge_adapter['code']:
            raise ValueError('本地充值适配代码回读失败')
        self.process.executable(recharge_entry, 4096)
        self.process.write(self.recharge_adapter['patch_va'], self.recharge_adapter['after'])
        if self.process.read(self.recharge_adapter['patch_va'], 4) != self.recharge_adapter['after']:
            raise ValueError('本地充值 GFx 回调安装失败')
        from codol_rebuild.application.accounts.purchase import RECHARGE_AMOUNT
        self.record({'local_recharge_adapter': {'entry': hex(recharge_entry), 'amount': RECHARGE_AMOUNT}})

    def _install_purchase_adapter(self, profile_data_address):
        """安装购买和库存刷新回调，初始化首次刷新所需的通信状态。"""
        from codol_rebuild.adapters.game.v1649370.accounts import purchase
        purchase.validate_contract(self.process.read)
        purchase_entry = self.process.allocate(4096)
        purchase_state = self.process.allocate(4096)
        self.purchase_adapter = purchase.build_adapter(purchase_entry, purchase_state, profile_data_address + 72)
        # 首次购买之前也可能请求刷新，因此安装时即初始化刷新状态。
        purchase.checked_write(self.process.read, self.process.write, purchase_state+96,
                               struct.pack('<IIII', 0xffffffff, 0xffffffff, 0, 1)+bytes(16))
        purchase.checked_write(self.process.read, self.process.write, purchase_entry, self.purchase_adapter['code'])
        self.process.executable(purchase_entry, 4096)
        purchase.checked_write(self.process.read, self.process.write, purchase.HANDLER_SLOT, self.purchase_adapter['after'])
        purchase.checked_write(self.process.read, self.process.write, purchase.REFRESH_SLOT, self.purchase_adapter['refresh_after'])
        self.record({'local_purchase_adapter': {'entry': hex(purchase_entry),
                     'policy': 'GP/CP products and bundles; permanent equipment; cumulative consumables'}})

    def _install_lobby_entry(self, lobby_entry, initializer_address, profile_data_address, room_plan):
        """连接大厅预览入口与联机帧轮询，最后发布原生调用边。"""
        # 所有被调用的适配先安装完成，再替换大厅调用边。
        # 联机帧轮询依赖房间适配和昵称轮询入口；离线直接进入预览辅助代码。
        from codol_rebuild.adapters.game.v1649370.frontend.menu_preview import build_menu_preview_helper
        plan = build_menu_preview_helper(lobby_entry, initializer_va=initializer_address,
                                         initializer_ready_va=profile_data_address + 72 if self.args.local_profile else None,
                                         activation_ready_va=self.multiplayer.state+20 if self.multiplayer else None)
        code = plan['entry_bytes']
        if len(code) > 4096 or self.process.read(plan['patch_va'], len(plan['patch_before'])) != plan['patch_before']:
            raise ValueError('离线UI helper入口或原始调用不符')
        if self.process.read(layout.CONNECTING_BRANCH, 2) != bytes.fromhex('7404') or self.process.read(layout.CONNECTING_FALSE, 2) != bytes.fromhex('32c0'):
            raise ValueError('原生连接提示false分支字节不符')
        self.process.write(lobby_entry, code)
        if self.process.read(lobby_entry, len(code)) != code:
            raise ValueError('离线UI helper代码回读不符')
        self.process.executable(lobby_entry, 4096)
        if self.multiplayer:
            broadcast_poll = self.broadcast_bridge.install() if self.broadcast_bridge is not None else None
            chat_poll = self.chat_bridge.install() if self.chat_bridge is not None else None
            from codol_rebuild.adapters.game.v1649370.session.session_tick import build_tick
            from codol_rebuild.adapters.game.v1649370.session.online_session import OnlineSessionBridge
            from codol_rebuild.adapters.game.v1649370.native_state import checked_write
            room_publication = self.multiplayer.install_adapters(room_plan)
            self.online_bridge = OnlineSessionBridge(
                self.process, self.online_session, room_publication['state'], self._online_connected, self.record)
            tick = self.process.allocate(4096)
            tick_plan = build_tick(tick, room_publication['state'], profile_data_address + 72,
                                   lobby_entry, room_publication['entries'], room_publication['list_publish'],
                                   self.name_adapter.get('poll'), broadcast_poll=broadcast_poll, chat_poll=chat_poll)
            if len(tick_plan['code']) > 4096:
                raise ValueError('会话帧桥超出代码页')
            checked_write(self.process, tick, tick_plan['code'])
            self.process.executable(tick, 4096)
            self.session_traps = {address: phase for address, phase in tick_plan['traps'].items()
                                  if phase == 'session_poll'}
            self.multiplayer.register_frame_traps({address: phase for address, phase in tick_plan['traps'].items()
                                                  if phase != 'session_poll'})
            self.multiplayer.install_settings_detours()
            self.record({'multiplayer_settings_detours': True})
            self.preview_caller_return = tick+9
            plan['patch_after'] = b'\xe8' + struct.pack('<I', (tick-plan['patch_va']-5) & 0xffffffff)
        self.process.write(plan['patch_va'], plan['patch_after'])
        self.process.write(layout.CONNECTING_BRANCH, bytes.fromhex('eb04'))
        if self.process.read(plan['patch_va'], len(plan['patch_after'])) != plan['patch_after'] or self.process.read(layout.CONNECTING_BRANCH, 2) != bytes.fromhex('eb04'):
            raise ValueError('离线UI试验补丁回读失败')
        for phase in ('before', 'after'):
            self.preview_traps[lobby_entry + plan[phase + '_trap_offset']] = phase
        self.preview_metadata.update(plan.get('metadata', {}))
        self.record({'offline_menu_preview_installed': True, 'helper': hex(lobby_entry), 'helper_sha256': hashlib.sha256(code).hexdigest(), 'call_va': hex(plan['patch_va']), 'call_before': plan['patch_before'].hex(), 'call_after': plan['patch_after'].hex(), 'connecting_branch': {'va': '0x60c6f7', 'before': '7404', 'after': 'eb04'}, 'metadata': self.preview_metadata})
        (self.run_dir / 'offline-menu-preview-helper.bin').write_bytes(code)
        self.log['offline_menu_preview'] = True

    def observe_state(self):
        pointer = struct.unpack('<I', self.process.read(layout.DW_DVAR, 4))[0]
        display = struct.unpack('<I', self.process.read(layout.DISPLAY_MODE, 4))[0]
        slot = struct.unpack('<I', self.process.read(layout.CLIENT_SLOT, 4))[0]
        encoded = struct.unpack('<I', self.process.read(layout.CLIENT_POINTERS + slot * 4, 4))[0] if slot < 8 else None
        decoded = encoded ^ layout.CLIENT_POINTER_XOR if encoded else None
        allocated = struct.unpack('<I', self.process.read(layout.CLIENT_ALLOCATED, 4))[0]
        ready = False
        if allocated == 1 and decoded and (65536 <= decoded <= 4293314527):
            try:
                self.process.read(decoded + 815632, 4)
                ready = True
            except OSError:
                pass
        return {'ui_resource_count': struct.unpack('<I', self.process.read(layout.UI_RESOURCE_COUNT, 4))[0], 'dw_dvar_pointer': hex(pointer), 'dw_enabled': self.process.read(pointer + 12, 1)[0] if pointer else None, 'display_mode_pointer': hex(display), 'display_mode': struct.unpack('<I', self.process.read(display + 12, 4))[0] if display else None, 'client_object_slot': slot, 'client_object_encoded': hex(encoded) if encoded is not None else None, 'client_object_decoded': hex(decoded) if decoded is not None else None, 'allocation_complete': allocated, 'client_object_ready': ready}

    def install_navigation_resource(self):
        """在原版主电影解析前安装已校验的会话内资源补丁，并回读数据及长度。"""
        from codol_rebuild.adapters.game.v1649370.session.ui_movie import find_resource_patch
        plan = find_resource_patch(self.process.read, self.process.regions())
        if self.process.read(plan['header_address'], 16) != plan['header_before']:
            raise ValueError('大厅资源头在安装前改变')
        self.process.write(plan['data_address'], plan['data_after'])
        if self.process.read(plan['data_address'], len(plan['data_after'])) != plan['data_after']:
            raise ValueError('大厅资源补丁回读失败')
        self.process.write(plan['length_address'], plan['length_after'])
        if self.process.read(plan['length_address'], 4) != plan['length_after']:
            raise ValueError('大厅资源长度回读失败')
        return {'installed': True, 'header': hex(plan['header_address']), 'compressed_size': len(plan['data_after']), 'patched_sha256': plan['patched_sha256'], 'patched_methods': ['MatchMakingStripView.onGetPlayerWarGodData', 'SanitizeController.sanitize', 'WeaponLoadOutController.onSetLoadoutNameHandle', 'MarketController.handleRedeemChangeName', 'MarketController.onRedeemChangeNameComplete', 'PlayerObj.getObj']}

    def capture_schema_scan(self):
        """在暂停点扫描 version+checksum 指纹，命中已重定位定义对象才导出结构。"""
        from codol_rebuild.adapters.game.profile_format import scan as schema_scan
        from codol_rebuild.adapters.game.profile_format.schema import capture_schema

        def safe_read(addr, size):
            try:
                return self.process.read(addr, size)
            except ValueError as exc:
                raise OSError(str(exc))
        regions = self.process.regions()
        matches = schema_scan.find_fingerprints(safe_read, regions, layout.KNOWN_DEFINITIONS)
        result = {'regions': len(regions), 'scanned_bytes': sum((size for _, size in regions)), 'definitions': {}}
        for label, addresses in matches.items():
            chosen = schema_scan.select_definition(safe_read, addresses, max_profile_size=layout.BUFFER_CAPACITY)
            entry = {'matches': [hex(a) for a in addresses], 'selected': hex(chosen['selected']) if chosen['selected'] is not None else None, 'candidates': [{**cand, 'address': hex(cand['address'])} for cand in chosen['candidates']]}
            if chosen['selected'] is not None:
                try:
                    entry['captured'] = capture_schema(safe_read, chosen['selected'], self.run_dir / 'schema' / label, max_profile_size=layout.BUFFER_CAPACITY)
                except (OSError, ValueError) as exc:
                    entry['capture_error'] = str(exc)
            result['definitions'][label] = entry
        return result

    def handle_exception(self, event, code, address, row):
        if code in (2147483651, 1073741855) and self.match_bridge and address in self.match_bridge.traps:
            self.match_bridge.handle(event, address)
            return True
        if code in (2147483651, 1073741855) and address in self.session_traps:
            if event.tid != self.process.main_thread_id:
                raise ValueError('公共会话调度必须在主线程执行')
            thread, context = self.process.context(event.tid)
            try:
                self.online_bridge.poll()
                self.realtime_dispatcher.poll()
                resume_address = self.match_bridge.poll(address+1) if self.match_bridge else address+1
                struct.pack_into('<I', context, 184, resume_address)
                self.process.set_context(thread, context)
            finally:
                self.process.close_thread(thread)
            return True
        if code in (2147483651, 1073741855) and self.chat_bridge and address in self.chat_bridge.traps:
            self.chat_bridge.handle(event, address)
            return True
        if code in (2147483651, 1073741855) and self.broadcast_bridge and address in self.broadcast_bridge.traps:
            self.broadcast_bridge.handle(event, address)
            return True
        if code in (2147483651, 1073741855) and self.multiplayer and address in self.multiplayer.traps:
            self.multiplayer.handle(event, address)
            return True
        if code in (2147483651, 1073741855) and self.purchase_adapter and address in self.purchase_adapter['traps']:
            from codol_rebuild.adapters.game.v1649370.accounts import purchase
            if event.tid != self.process.main_thread_id or self.observe_state()['dw_enabled'] != 0:
                raise ValueError('本地购买必须在离线主线程执行')
            phase = self.purchase_adapter['traps'][address]
            state = self.purchase_adapter['state']
            thread, ctx = self.process.context(event.tid)
            try:
                frame = struct.unpack_from('<I', ctx, 180)[0]
                if frame != struct.unpack_from('<I', ctx, 196)[0]:
                    raise ValueError('购买桥保存帧不匹配')
                try:
                    if phase == 'request':
                        payload = bytearray(512)
                        struct.pack_into('<IIII', payload, 96, 0xffffffff, 0xffffffff, 0, 1)
                        purchase.checked_write(self.process.read, self.process.write, state, bytes(payload))
                        sku, quantity = purchase.request(self.process.read, frame)
                        if not self.profiles.ready:
                            raise ValueError('本地档案尚未就绪')
                        purchase.checked_write(self.process.read, self.process.write, state, struct.pack('<III', 1, sku, quantity))
                    elif phase == 'quoted':
                        offer = self.profiles.quote(state)
                        self.profiles.purchase(offer, purchase.u32(self.process.read, state+8))
                        purchase.checked_write(self.process.read, self.process.write, state+16, struct.pack('<I', 1))
                        row['local_purchase_committed'] = offer
                    else:
                        row['local_purchase_refreshed'] = self.profiles.inventory_count
                except self.account_errors as error:
                    purchase.set_failure(self.process.read, self.process.write, state, error)
                    row['local_purchase_error'] = str(error)
                struct.pack_into('<I', ctx, 184, address+1)
                self.process.set_context(thread, ctx)
            finally:
                self.process.close_thread(thread)
            self.record(row)
        elif code in (2147483651, 1073741855) and self.recharge_adapter and address == self.recharge_adapter['trap']:
            from codol_rebuild.adapters.game.v1649370.accounts import ui_recharge
            if event.tid != self.process.main_thread_id or self.observe_state()['dw_enabled'] != 0:
                raise ValueError('本地充值必须在离线主线程执行')
            thread, ctx = self.process.context(event.tid)
            try:
                frame = struct.unpack_from('<I', ctx, 180)[0]
                if frame != struct.unpack_from('<I', ctx, 196)[0]:
                    raise ValueError('充值桥保存帧不匹配')
                result = ui_recharge.result_slot(self.process.read, frame)
                ui_recharge.reply(self.process.read, self.process.write, result, -1)
                try:
                    if not self.profiles.ready:
                        raise ValueError('本地档案尚未就绪')
                    from codol_rebuild.application.accounts.purchase import RECHARGE_AMOUNT
                    balances = ui_recharge.credit(
                        self.process.read, self.process.write,
                        lambda before: {**before, 'cp': self.profiles.credit_state(
                            'cp', RECHARGE_AMOUNT, before['cp'])['cp']},
                        self.profiles.commit_balances)
                except self.account_errors as error:
                    row['local_recharge_error'] = str(error)
                else:
                    ui_recharge.reply(self.process.read, self.process.write, result, balances['cp'])
                    row['local_recharge_saved'] = balances
                struct.pack_into('<I', ctx, 184, address+1)
                self.process.set_context(thread, ctx)
            finally:
                self.process.close_thread(thread)
            self.record(row)
        elif code in (2147483651, 1073741855) and self.name_adapter and (address in self.name_adapter['traps']):
            from codol_rebuild.adapters.game.v1649370.accounts import player_name
            if event.tid != self.process.main_thread_id or self.observe_state()['dw_enabled'] != 0:
                raise ValueError('本地改名必须在离线主线程执行')
            phase = self.name_adapter['traps'][address]
            state = self.name_adapter['state']
            thread, ctx = self.process.context(event.tid)
            try:
                frame = struct.unpack_from('<I', ctx, 180)[0]
                if frame != struct.unpack_from('<I', ctx, 196)[0]:
                    raise ValueError('昵称桥保存帧不匹配')
                row['local_player_name_phase'] = phase
                if self.server_name_bridge is not None and phase != 'restored':
                    self.server_name_bridge.handle(phase, frame, row)
                elif phase == 'request':
                    self.pending_name = None
                    try:
                        value = player_name.read_request(self.process.read, frame, self.name_adapter['ready'])
                        self.profiles.validate_name(value)
                        player_name.stage_name(self.process.read, self.process.write, state, value)
                        self.pending_name = value
                    except (OSError, ValueError) as error:
                        self.process.write(state, bytes(8))
                        row['local_player_name_error'] = str(error)
                elif phase == 'applied':
                    try:
                        if self.pending_name is None or self.profiles.name_snapshot is None:
                            raise ValueError('昵称请求没有对应宿主状态')
                        player_name.verify_applied(self.process.read, state)
                        self.profiles.rename(self.pending_name)
                    except self.account_errors as error:
                        row['local_player_name_error'] = str(error)
                        self.process.write(state + 4, bytes(4))
                    else:
                        self.process.write(state + 4, struct.pack('<I', 1))
                        if self.process.read(state + 4, 4) != struct.pack('<I', 1):
                            raise ValueError('昵称提交确认回读失败')
                        row['local_player_name_saved'] = self.profiles.name_snapshot.copy()
                        self.pending_name = None
                elif phase == 'rolled_back':
                    player_name.verify_applied(self.process.read, state, rollback=True)
                    self.pending_name = None
                    row['local_player_name_rolled_back'] = True
                elif phase == 'restored':
                    player_name.verify_applied(self.process.read, state)
                    self.process.write(state, bytes(8))
                    row['local_player_name_restored'] = self.profiles.name_snapshot.copy()
                else:
                    raise ValueError('未知昵称桥阶段')
                struct.pack_into('<I', ctx, 184, address + 1)
                self.process.set_context(thread, ctx)
            finally:
                self.process.close_thread(thread)
            self.record(row)
        elif code in (2147483651, 1073741855) and address in self.profile_traps:
            if event.tid != self.process.main_thread_id or self.observe_state()['dw_enabled'] != 0:
                raise ValueError('Local profile bridge requires offline main thread')
            profile_data, phase, pool = self.profile_traps[address]
            if phase == 'prepare':
                self.profiles.prepare_native(profile_data, pool, self.run_dir / 'local-profile', self.name_adapter['state'])
            else:
                self.profiles.complete_native(profile_data, pool, self.purchase_adapter is not None)
            thread, ctx = self.process.context(event.tid)
            try:
                struct.pack_into('<I', ctx, 184, address + 1)
                self.process.set_context(thread, ctx)
            finally:
                self.process.close_thread(thread)
            self.record({'local_profile_phase': phase, 'local_profile': self.log.get('local_profile')})
        elif code in (2147483651, 1073741855) and address in self.preview_traps:
            phase = self.preview_traps[address]
            thread, ctx = self.process.context(event.tid)
            try:
                eax = struct.unpack_from('<I', ctx, 176)[0]
                esp = struct.unpack_from('<I', ctx, 196)[0]
                row['offline_menu_preview_phase'] = phase
                row['eax'] = hex(eax)
                row['observed_state'] = self.observe_state()
                row['saved_getter_eax'] = struct.unpack('<I', self.process.read(esp + 28, 4))[0]
                row['saved_caller_return'] = hex(struct.unpack('<I', self.process.read(esp + 36, 4))[0])
                row['catalog'] = {'initialized': self.process.read(layout.CATALOG_READY, 1)[0], 'count': struct.unpack('<I', self.process.read(layout.CATALOG_COUNT, 4))[0], 'records': hex(struct.unpack('<I', self.process.read(layout.CATALOG_RECORDS, 4))[0])}
                row['native_cpp_activate_called'] = self.process.read(layout.UI_ACTIVATED, 1)[0]
                row['root_gfx_value'] = {'type': struct.unpack('<I', self.process.read(layout.ROOT_TYPE, 4))[0], 'interface': hex(struct.unpack('<I', self.process.read(layout.ROOT_INTERFACE, 4))[0]), 'value': hex(struct.unpack('<I', self.process.read(layout.ROOT_VALUE, 4))[0])}
                if event.tid != self.process.main_thread_id or row['observed_state']['dw_enabled'] != 0:
                    raise ValueError('离线UI激活必须发生于本次主线程且DW关闭')
                if row['saved_getter_eax'] != 1 or row['saved_caller_return'] != hex(self.preview_caller_return):
                    raise ValueError('离线UI helper调用栈或原getter结果不符')
                if phase == 'before':
                    if self.args.local_profile:
                        from codol_rebuild.adapters.game.v1649370.frontend.viewer import view_state
                        row['local_view_state'] = view_state(self.process.read)
                        if row['local_view_state']['viewer_initialized'] != 1 or row['local_view_state']['weapon_definition_count'] <= 0:
                            raise ValueError('Local UI activation preceded native weapon/viewer readiness')
                    if row['catalog']['initialized'] != 1 or row['catalog']['count'] <= 0 or row['catalog']['records'] == '0x0' or (row['native_cpp_activate_called'] != 0) or (not row['observed_state']['client_object_ready']):
                        raise ValueError('离线UI激活真实本地对象尚未就绪')
                    row['activation_cycle'] = self.preview_cycles.begin(row['native_cpp_activate_called'])
                    self.log['offline_menu_preview_activation_count'] = self.preview_cycles.count
                    self.log['offline_menu_preview_attempted'] = True
                else:
                    self.preview_cycles.complete(eax & 255, row['native_cpp_activate_called'])
                    row['activation_cycle'] = self.preview_cycles.count
                    row['native_invoke_result'] = eax & 255
                    self.log['offline_menu_preview_returned'] = True
                    self.log['offline_menu_preview_invoke_result'] = eax & 255
                struct.pack_into('<I', ctx, 184, address + 1)
                self.process.set_context(thread, ctx)
            finally:
                self.process.close_thread(thread)
            self.record(row)
        else:
            return False
        return True

    def breakpoint(self, event, address, original, label, row, thread, ctx):
        esp = struct.unpack_from('<I', ctx, 196)[0]
        row.update({'milestone': label, 'stack': self.process.read(esp, 128).hex(), 'registers': {n: hex(struct.unpack_from('<I', ctx, o)[0]) for n, o in [('eax', 176), ('ecx', 172), ('edx', 168), ('ebx', 164), ('esi', 160), ('edi', 156), ('ebp', 180), ('esp', 196)]}})
        if label in ('Fatal error', 'Com Error'):
            row['argument_strings'] = {}
            for index, pointer in enumerate(struct.unpack('<8I', self.process.read(esp + 4, 32))):
                if pointer < 65536:
                    continue
                try:
                    row['argument_strings'][str(index + 1)] = self.process.cstring(pointer)
                except OSError:
                    pass
        if label.startswith('view-result:'):
            from codol_rebuild.adapters.game.v1649370.frontend.viewer import view_result, view_state
            row['local_view_state'] = view_state(self.process.read)
            try:
                row['view_result'] = view_result(self.process.read, struct.unpack_from('<I', ctx, 180)[0])
            except (OSError, ValueError) as error:
                row['view_result_error'] = str(error)
        if label.startswith('view:'):
            from codol_rebuild.adapters.game.v1649370.frontend.viewer import view_state
            row['local_view_state'] = view_state(self.process.read)
            if label != 'view:UIViewerFrame':
                try:
                    params = struct.unpack('<I', self.process.read(esp + 4, 4))[0]
                    values, args_pointer, count = struct.unpack('<3I', self.process.read(params + 12, 12))
                    row['view_argument_count'] = count
                    if count <= 8 and args_pointer:
                        row['view_arguments'] = [{'type': struct.unpack('<I', self.process.read(args_pointer + i * 32 + 12, 4))[0], 'value': struct.unpack('<I', self.process.read(args_pointer + i * 32 + 16, 4))[0]} for i in range(count)]
                except (OSError, ValueError) as error:
                    row['view_arguments_error'] = str(error)
        if label in ('WinMain', 'Com Init'):
            pointer = struct.unpack('<I', self.process.read(esp + 12, 4))[0] if label == 'WinMain' else layout.STARTUP_ARGUMENTS
            row['startup_arguments'] = self.process.cstring(pointer, strict=True)
        if label in ('Frontend tick', 'Switch to main menu', 'Engine init returned'):
            row['observed_state'] = self.observe_state()
        if label == 'Engine init returned':
            self.log['engine_init_returned'] = True
            self.phase_done('engine-init')
        if label == 'CRT entry' and getattr(self.args, 'game_network_mode', None) == 'local':
            from codol_rebuild.adapters.game.v1649370.matches.network_initialization import install
            self.log['native_network_initialization'] = install(self.process)
            self.record({'native_network_initialization': self.log['native_network_initialization']})
        if label == 'CRT entry' and self.args.trace_files:
            self.debug.install_file_trace()
        if label == 'CRT entry' and self.args.offline_menu_preview:
            self.install_lobby_adapters()
        if label == 'Local navigation resource':
            row['local_navigation_resource'] = self.install_navigation_resource()
        if label.startswith('MessageBox'):
            values = struct.unpack('<4I', self.process.read(esp, 16))
            row['text'] = self.process.cstring(values[2], label.endswith('W'))
            row['caption'] = self.process.cstring(values[3], label.endswith('W'))
            if '错误' in row['caption'] or 'error' in row['caption'].lower():
                self.log['stop_reason'] = 'fatal_dialog'
        if self.args.capture_schema and (not self.schema_captured) and (label in ('Engine init returned', 'Frontend tick', 'Switch to main menu')):
            try:
                row['schema_capture'] = self.capture_schema_scan()
                if any((item.get('captured') for item in row['schema_capture']['definitions'].values())):
                    self.schema_captured=True
                    self.log['schema_captured'] = True
            except OSError as error:
                row['schema_capture_error'] = str(error)
        self.process.write(address, original)
        struct.pack_into('<I', ctx, 184, address)
        self.process.set_context(thread, ctx)
