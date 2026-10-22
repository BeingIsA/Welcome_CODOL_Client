"""1649370 会话桥：版本补丁安装、原生事件解释和 UI 完成时序。"""
import hashlib, struct
from codol_rebuild.runtime.contracts import ProcessAccess, DebugControl
from codol_rebuild.adapters.game.v1649370 import layout
from codol_rebuild.adapters.game.v1649370.menu_preview import PreviewActivationCycles
from codol_rebuild.adapters.game.v1649370.profile_bridge import LocalProfileBridge

class GameSessionBridge:
    """仅拥有版本状态；进程和账号实现由装配入口注入。"""
    def __init__(self, args, run_dir, image, pe, log, record, phase_done, process: ProcessAccess, account_factory, account_errors, multiplayer=None):
        self.args=args; self.run_dir=run_dir; self.image=image; self.pe=pe
        self.log=log; self.record=record; self.phase_done=phase_done; self.process=process
        self.account_factory=account_factory; self.account_errors=account_errors
        self.preview_traps={}; self.preview_metadata={}; self.preview_cycles=PreviewActivationCycles()
        self.profile_traps={}; self.name_adapter=None; self.pending_name=None
        self.recharge_adapter=None
        self.purchase_adapter=None
        self.profiles=LocalProfileBridge(process, account_factory, log, record)
        self.debug=None
        self.engine_ready=False; self.menu_queued=False; self.schema_captured=False
        self.preview_caller_return = 0x5EBFF8
        self.multiplayer = None
        if multiplayer is not None:
            from codol_rebuild.adapters.game.v1649370.multiplayer_bridge import MultiplayerBridge
            self.multiplayer = MultiplayerBridge(process, multiplayer, record)

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
        if self.args.queue_main_menu:
            self.debug.arm(layout.ENGINE_RETURN,'Engine init returned'); self.debug.arm(layout.MAIN_MENU,'Switch to main menu')
        if self.args.capture_schema:
            self.debug.arm(layout.FRONTEND_TICK,'Frontend tick'); self.debug.arm(layout.MAIN_MENU,'Switch to main menu')
        for value in self.args.breakpoint:
            address,label=value.split(':',1);self.debug.arm(int(address,0),label)

    def install_menu_preview(self):
        from codol_rebuild.adapters.game.v1649370.menu_preview import build_menu_preview_helper, validate_menu_preview_image
        validate_menu_preview_image(self.image.read_bytes())
        entry = self.process.allocate(4096)
        initializer = None
        if self.args.local_profile:
            from codol_rebuild.adapters.game.v1649370 import ui_movie
            if self.process.read(ui_movie.LOAD_SITE, len(ui_movie.LOAD_BYTES)) != ui_movie.LOAD_BYTES:
                raise ValueError('大厅 UI 加载调用边不匹配')
            self.debug.arm(ui_movie.LOAD_SITE, 'Local navigation resource')
            from codol_rebuild.adapters.game.v1649370.local_ui_services import local_optional_service_patches, local_welcome_page_patch
            welcome_patch = local_welcome_page_patch()
            local_patches = local_optional_service_patches() + [welcome_patch]
            for va, before, after in local_patches:
                if self.process.read(va, len(before)) != before:
                    raise ValueError(f'Local callback mismatch at {va:#x}')
            for va, before, after in local_patches:
                self.process.write(va, after)
                if self.process.read(va, len(after)) != after:
                    raise ValueError(f'Local callback readback mismatch at {va:#x}')
            self.record({'local_unsupported_services': ['WarGod credit rating'], 'behavior': 'void request ignored; no score fabricated'})
            self.record({'local_welcome_auto_popup_disabled': True, 'callback': 'IsWelcomePageShowed', 'patch_va': hex(welcome_patch[0]), 'behavior': 'report welcome already shown; manual MOTD entry unchanged'})
            from codol_rebuild.adapters.game.v1649370.viewer import VIEW_TRACE_POINTS, VIEW_RESULT_POINTS
            for address, label in (VIEW_TRACE_POINTS | VIEW_RESULT_POINTS).items():
                self.debug.arm(address, label)
            from codol_rebuild.adapters.game.v1649370.profile import build_initializer, inventory_relocations, CAPACITY, STRIDE, INITIALIZER_DATA_SIZE
            from codol_rebuild.adapters.game.v1649370.profile import validate_account_native_contract
            validate_account_native_contract(self.process.read)
            initializer = self.process.allocate(4096)
            profile_data = self.process.allocate(INITIALIZER_DATA_SIZE)
            pool_allocation = self.process.allocate(CAPACITY * STRIDE + 32)
            pool = pool_allocation + 16
            self.process.write(pool_allocation, b'LOCAL-INVENTORY!')
            self.process.write(pool + CAPACITY * STRIDE, b'LOCAL-INVENTORY!')
            relocations = inventory_relocations(pool)
            for va, before, after in relocations:
                if self.process.read(va, len(before)) != before:
                    raise ValueError(f'Inventory operand mismatch at {va:#x}')
            for va, before, after in relocations:
                self.process.write(va, after)
            bridge = build_initializer(initializer, profile_data, pool)
            self.process.write(initializer, bridge['code'])
            self.process.write(profile_data, bridge['data'])
            from codol_rebuild.adapters.game.v1649370.profile import build_stats_ready_adapter
            stats_entry = initializer + 3072
            stats = build_stats_ready_adapter(stats_entry, profile_data)
            if len(bridge['code']) > 3072 or self.process.read(stats['patch_va'], 5) != stats['before']:
                raise ValueError('Local UI stats adapter image/region mismatch')
            self.process.write(stats_entry, stats['code'])
            self.process.write(stats['patch_va'], stats['after'])
            if self.process.read(stats_entry, len(stats['code'])) != stats['code'] or self.process.read(stats['patch_va'], 5) != stats['after']:
                raise ValueError('Local UI stats adapter readback mismatch')
            from codol_rebuild.adapters.game.v1649370.viewer import build_local_viewer_adapter
            viewer_entry = initializer + 3328
            viewer = build_local_viewer_adapter(viewer_entry, profile_data + 72)
            if len(stats['code']) > 256 or len(viewer['code']) > 768 or self.process.read(viewer['patch_va'], len(viewer['gate_before'])) != viewer['gate_before']:
                raise ValueError('Local viewer adapter image/region mismatch')
            self.process.write(viewer_entry, viewer['code'])
            self.process.write(viewer['patch_va'], viewer['after'])
            if self.process.read(viewer_entry, len(viewer['code'])) != viewer['code'] or self.process.read(viewer['patch_va'], 5) != viewer['after']:
                raise ValueError('Local viewer adapter readback mismatch')
            self.log['local_viewer_adapter'] = {'patch_va': hex(viewer['patch_va']), 'local_modes': viewer['local_modes'], 'body_va': hex(viewer['body_va']), 'exit_va': hex(viewer['exit_va'])}
            self.record({'local_viewer_adapter': self.log['local_viewer_adapter']})
            from codol_rebuild.adapters.game.v1649370.playlist import build_local_playlist_adapter, validate_local_playlist_contract
            validate_local_playlist_contract(self.process.read)
            playlist_entry = self.process.allocate(4096)
            playlist = build_local_playlist_adapter(playlist_entry, profile_data + 72)
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
            self.log['local_playlist_adapter'] = {'entry': hex(playlist_entry), 'patches': [hex(p['va']) for p in playlist['patches']], 'policy': 'local ready offline, no season and zero configured playlist: UI ID -1; local -1 is invalid'}
            self.record({'local_playlist_adapter': self.log['local_playlist_adapter']})
            from codol_rebuild.adapters.game.v1649370.room_name import build_room_name_adapter, validate_room_name_contract
            validate_room_name_contract(self.process.read)
            room_name_entry = self.process.allocate(4096)
            room_name = build_room_name_adapter(room_name_entry, profile_data + 72)
            self.process.write(room_name_entry, room_name['code'])
            if self.process.read(room_name_entry, len(room_name['code'])) != room_name['code']:
                raise ValueError('Room name adapter readback mismatch')
            self.process.executable(room_name_entry, 4096)
            self.process.write(room_name['patch_va'], room_name['after'])
            if self.process.read(room_name['patch_va'], 5) != room_name['after']:
                raise ValueError('Room name call edge readback mismatch')
            self.record({'local_room_name_adapter': {'entry': hex(room_name_entry), 'patch_va': hex(room_name['patch_va']), 'policy': 'local room names only; nonempty, <=255 UTF-8 bytes, no ASCII controls', 'completion': 'original cppRefreshTask; original UI task update'}})
            from codol_rebuild.adapters.game.v1649370.room import build_room_adapter, validate_room_contract
            validate_room_contract(self.process.read)
            room_entry = self.process.allocate(4096)
            room_state = self.process.allocate(4096)
            if self.multiplayer:
                self.multiplayer.allocate()
            room = build_room_adapter(room_entry, room_state, profile_data + 72,
                                      deferred=self.multiplayer is not None,
                                      identity_address=self.multiplayer.state+24 if self.multiplayer else None)
            if len(room['code']) > 4096:
                raise ValueError('Local room adapter exceeds allocation')
            self.process.write(room_entry, room['code'])
            if self.process.read(room_entry, len(room['code'])) != room['code']:
                raise ValueError('Local room adapter readback mismatch')
            self.process.executable(room_entry, 4096)
            for patch in room['patches']:
                self.process.write(patch['va'], patch['after'])
                if self.process.read(patch['va'], len(patch['after'])) != patch['after']:
                    raise ValueError('Local room patch readback mismatch')
            self.record({'local_room_adapter': {'entry': hex(room_entry), 'state': hex(room_state), 'patches': [hex(p['va']) for p in room['patches']], 'completion': 'original native session/create callbacks; local room ownership only'}})
            self.process.executable(initializer, 4096)
            self.profile_traps[bridge['trap']] = (profile_data, 'prepare', pool)
            self.profile_traps[bridge['after']] = (profile_data, 'after', pool)
            self.record({'local_profile_bridge': hex(initializer), 'trap': hex(bridge['trap'])})
            from codol_rebuild.adapters.game.v1649370 import player_name
            player_name.validate_contract(self.process.read)
            name_entry = self.process.allocate(4096)
            name_state = self.process.allocate(4096)
            self.name_adapter = player_name.build_adapter(name_entry, name_state, profile_data + 72, initializer)
            if len(self.name_adapter['code']) > 4096:
                raise ValueError('昵称适配超出代码页')
            self.process.write(name_entry, self.name_adapter['code'])
            if self.process.read(name_entry, len(self.name_adapter['code'])) != self.name_adapter['code']:
                raise ValueError('昵称适配代码回读失败')
            self.process.executable(name_entry, 4096)
            self.process.write(self.name_adapter['patch_va'], self.name_adapter['after'])
            if self.process.read(self.name_adapter['patch_va'], 4) != self.name_adapter['after']:
                raise ValueError('昵称GFx回调安装回读失败')
            initializer = self.name_adapter['initializer']
            self.record({'local_player_name_adapter': {'entry': hex(name_entry), 'state': hex(name_state), 'policy': 'local offline only; consume name card atomically; durable commit before success; restore on startup'}})
            from codol_rebuild.adapters.game.v1649370 import ui_recharge
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
            from codol_rebuild.adapters.game.v1649370 import purchase
            purchase.validate_contract(self.process.read)
            purchase_entry = self.process.allocate(4096)
            purchase_state = self.process.allocate(4096)
            self.purchase_adapter = purchase.build_adapter(purchase_entry, purchase_state, profile_data + 72)
            # Refresh may be requested before the first purchase.
            purchase.checked_write(self.process.read, self.process.write, purchase_state+96,
                                   struct.pack('<IIII', 0xffffffff, 0xffffffff, 0, 1)+bytes(16))
            purchase.checked_write(self.process.read, self.process.write, purchase_entry, self.purchase_adapter['code'])
            self.process.executable(purchase_entry, 4096)
            purchase.checked_write(self.process.read, self.process.write, purchase.HANDLER_SLOT, self.purchase_adapter['after'])
            purchase.checked_write(self.process.read, self.process.write, purchase.REFRESH_SLOT, self.purchase_adapter['refresh_after'])
            self.record({'local_purchase_adapter': {'entry': hex(purchase_entry),
                         'policy': 'GP/CP products and bundles; permanent equipment; cumulative consumables'}})
        plan = build_menu_preview_helper(entry, initializer_va=initializer,
                                         initializer_ready_va=profile_data + 72 if self.args.local_profile else None,
                                         activation_ready_va=self.multiplayer.state+20 if self.multiplayer else None)
        code = plan['entry_bytes']
        if len(code) > 4096 or self.process.read(plan['patch_va'], len(plan['patch_before'])) != plan['patch_before']:
            raise ValueError('离线UI helper入口或原始调用不符')
        if self.process.read(layout.CONNECTING_BRANCH, 2) != bytes.fromhex('7404') or self.process.read(layout.CONNECTING_FALSE, 2) != bytes.fromhex('32c0'):
            raise ValueError('原生连接提示false分支字节不符')
        self.process.write(entry, code)
        if self.process.read(entry, len(code)) != code:
            raise ValueError('离线UI helper代码回读不符')
        self.process.executable(entry, 4096)
        if self.multiplayer:
            tick = self.multiplayer.install_tick(profile_data+72, entry, room)
            self.preview_caller_return = tick+9
            plan['patch_after'] = b'\xe8' + struct.pack('<I', (tick-plan['patch_va']-5) & 0xffffffff)
        self.process.write(plan['patch_va'], plan['patch_after'])
        self.process.write(layout.CONNECTING_BRANCH, bytes.fromhex('eb04'))
        if self.process.read(plan['patch_va'], len(plan['patch_after'])) != plan['patch_after'] or self.process.read(layout.CONNECTING_BRANCH, 2) != bytes.fromhex('eb04'):
            raise ValueError('离线UI试验补丁回读失败')
        for phase in ('before', 'after'):
            self.preview_traps[entry + plan[phase + '_trap_offset']] = phase
        self.preview_metadata.update(plan.get('metadata', {}))
        self.record({'offline_menu_preview_installed': True, 'helper': hex(entry), 'helper_sha256': hashlib.sha256(code).hexdigest(), 'call_va': hex(plan['patch_va']), 'call_before': plan['patch_before'].hex(), 'call_after': plan['patch_after'].hex(), 'connecting_branch': {'va': '0x60c6f7', 'before': '7404', 'after': 'eb04'}, 'metadata': self.preview_metadata})
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
        from codol_rebuild.adapters.game.v1649370.ui_movie import find_resource_patch
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
        if code in (2147483651, 1073741855) and self.multiplayer and address in self.multiplayer.traps:
            self.multiplayer.handle(event, address)
            return True
        if code in (2147483651, 1073741855) and self.purchase_adapter and address in self.purchase_adapter['traps']:
            from codol_rebuild.adapters.game.v1649370 import purchase
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
            from codol_rebuild.adapters.game.v1649370 import ui_recharge
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
            from codol_rebuild.adapters.game.v1649370 import player_name
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
                if phase == 'request':
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
                        from codol_rebuild.adapters.game.v1649370.viewer import view_state
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
            from codol_rebuild.adapters.game.v1649370.viewer import view_result, view_state
            row['local_view_state'] = view_state(self.process.read)
            try:
                row['view_result'] = view_result(self.process.read, struct.unpack_from('<I', ctx, 180)[0])
            except (OSError, ValueError) as error:
                row['view_result_error'] = str(error)
        if label.startswith('view:'):
            from codol_rebuild.adapters.game.v1649370.viewer import view_state
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
        if label in ('Frontend tick', 'Switch to main menu', 'Engine init returned', 'Menu command queue'):
            row['observed_state'] = self.observe_state()
        if label == 'Engine init returned':
            self.engine_ready=True
            self.log['engine_init_returned'] = True
            self.phase_done('engine-init')
            if self.args.queue_main_menu:
                self.debug.arm(layout.COMMAND_QUEUE, 'Menu command queue')
        if label == 'Menu command queue':
            from codol_rebuild.adapters.game.v1649370.menu_queue import build_queue_plan
            values = struct.unpack('<3I', self.process.read(esp, 12))
            pointer, capacity, length = struct.unpack('<3I', self.process.read(layout.COMMAND_DESCRIPTOR, 12))
            state = {**row['observed_state'], 'thread_id': event.tid, 'main_thread_id': self.process.main_thread_id, 'return_address': values[0], 'local_client_index': values[1], 'controller_index': values[2], 'engine_returned': self.engine_ready, 'already_queued': self.menu_queued}
            try:
                if pointer != layout.COMMAND_BUFFER or capacity != 131072 or length >= capacity:
                    raise ValueError('命令缓冲结构不符合固定版本')
                critical = struct.unpack('<4I', self.process.read(layout.COMMAND_LOCK, 16))
                row['command_lock'] = {'recursion_count': critical[2], 'owning_thread': critical[3]}
                if critical[3] != event.tid or critical[2] < 1:
                    raise ValueError('命令临界区未由当前主线程持有')
                plan = build_queue_plan(pointer, capacity, length, self.process.read(pointer, length) if length else b'', state)
            except ValueError as error:
                row['menu_queue_refused'] = str(error)
            else:
                self.process.write(plan['payload_address'], plan['payload'])
                if self.process.read(plan['payload_address'], len(plan['payload'])) != plan['payload']:
                    raise ValueError('命令数据回读失败')
                self.process.write(plan['length_address'], plan['length_bytes'])
                if self.process.read(plan['length_address'], 4) != plan['length_bytes']:
                    raise ValueError('命令长度回读失败')
                self.menu_queued=True
                self.log['menu_command_queued'] = True
                row['menu_queue'] = {'new_length': plan['new_length'], 'payload_address': hex(plan['payload_address']), 'command': 'switchtomainmenu'}
        if label == 'CRT entry' and self.args.trace_files:
            self.debug.install_file_trace()
        if label == 'CRT entry' and self.args.offline_menu_preview:
            self.install_menu_preview()
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
