"""1649370 专用受控运行诊断：独立副本、低权限、断网守卫及真实调试事件。

Win32 结构和低权限令牌流程参考旧 Client/infra/win32_api.py 与
v2697/session.py；不复用任何 2697 游戏地址、就绪标志或大厅补丁。
每次运行留存输入指纹、事件和异常；进程存在不等于主菜单可用。
"""
from __future__ import annotations
from codol_rebuild.core.paths import REBUILD_ROOT as REBUILD_ROOT_PATH
import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import traceback

import pefile
from codol_rebuild.client.v1649370 import layout
from codol_rebuild.resources.runtime_cache import prepare_cache, link_resource
from codol_rebuild.platform.resource_cache_acl import protect_cache

ROOT = REBUILD_ROOT_PATH
from codol_rebuild.platform.win32 import (K, A, PTR, SIZE, api, check, close, wait, vquery,
    SI, PI, EVENT, MBI, SHELLINFO, low_token, token_info, current_sid)
FORBIDDEN = {'codomp_client_shipretailbase.dll', 'tersafe.dll', 'tenslx.dll', 'tenproxy.dll',
             'gamerpcs.dll', 'tenrpcs.dll', 'ace-ats32.dll', 'ace-drv32.dll', 'vsddrvdll.dll',
             'policyprobe.dll', 'tp3helper.exe', 'gbspy.dll', 'tcj.dll'}
DEPENDENCIES = ('GCloudVoice.dll', 'bink2w32.dll', 'dbghelp.dll', 'libcef.dll', 'mss32.dll',
                'D3DX9_43.dll', 'd3dcompiler_43.dll', 'libEGL.dll', 'libGLESv2.dll', 'icudtl.dat',
                'natives_blob.bin', 'snapshot_blob.bin', 'cef.pak', 'cef_100_percent.pak',
                'cef_200_percent.pak', 'cef_extensions.pak', 'localization.txt', 'logo.bmp', 'splash.bmp')


def digest(path):
    """计算输入或候选的文件指纹。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(args):
    """建立单次诊断会话；守卫 ready 前不执行客户端主线程。"""
    run_dir=(ROOT/'workspaces'/'offline-runs'/args.name).resolve()
    allowed=(ROOT/'workspaces'/'offline-runs').resolve()
    if not run_dir.is_relative_to(allowed) or run_dir == allowed or run_dir.exists():
        raise ValueError('必须使用 offline-runs 下尚不存在的单次会话名')
    run_dir.mkdir(parents=True)
    stage=run_dir/'runtime'
    stage.mkdir()
    log={'scope':'offline client startup diagnostic','main_menu_verified':False,'modules':[], 'events':[], 'started_at':time.time()}
    pi=PI(); token=None; guard=None; pending=None; breaks={}; rearm={}; tids=set()
    file_traps={}; preview_traps={}; preview_metadata={}; supplier=None
    from codol_rebuild.client.v1649370.menu_preview import PreviewActivationCycles
    preview_cycles=PreviewActivationCycles()
    profile_traps={}
    name_adapter=None;name_snapshot=None;pending_name=None
    local_account=None;profile_repository=None;profile_schemas=None
    next_profile_save=0
    settings_session=None;next_settings_save=0
    pending_mounts=[];shared_overlay={};phase_started=time.monotonic()
    log_file=(run_dir/'events.jsonl').open('x',encoding='utf-8')
    def record(row):
        row['time']=time.time(); log['events'].append(row)
        log_file.write(json.dumps(row,ensure_ascii=False)+'\n'); log_file.flush()
    def phase_done(name):
        nonlocal phase_started
        now=time.monotonic()
        row={'phase':name,'seconds':round(now-phase_started,3)}
        log.setdefault('startup_timings',[]).append(row);record({'startup_timing':row})
        print('[startup] '+name+': '+str(row['seconds'])+'s',flush=True)
        phase_started=now
    def save_game_settings(final=False):
        nonlocal next_settings_save
        if settings_session is None or not log.get('resumed'):return
        if not final and time.monotonic()<next_settings_save:return
        next_settings_save=time.monotonic()+2
        try:
            changed=settings_session.checkpoint(final=final)
            if changed:
                log['game_settings_saved']={'files':changed,'time':time.time(),'final':final}
                record({'game_settings_saved':log['game_settings_saved']})
                log.pop('game_settings_save_error',None)
        except (OSError,ValueError,sqlite3.Error) as error:
            message=str(error)
            if log.get('game_settings_save_error')!=message:record({'game_settings_save_error':message})
            log['game_settings_save_error']=message
    def mem(address,size):
        buf=C.create_string_buffer(size); count=SIZE()
        check(api(K,'ReadProcessMemory',W.BOOL,PTR,PTR,PTR,SIZE,C.POINTER(SIZE))(pi.process,address,buf,size,C.byref(count)))
        if count.value!=size: raise ValueError('部分内存读取')
        return buf.raw
    def write(address,data):
        old=W.DWORD(); count=SIZE()
        check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,address,len(data),64,C.byref(old)))
        try:
            check(api(K,'WriteProcessMemory',W.BOOL,PTR,PTR,PTR,SIZE,C.POINTER(SIZE))(pi.process,address,C.c_char_p(data),len(data),C.byref(count)))
            if count.value!=len(data): raise ValueError('部分内存写入')
            check(api(K,'FlushInstructionCache',W.BOOL,PTR,PTR,SIZE)(pi.process,address,len(data)))
        finally:
            ignored=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,address,len(data),old.value,C.byref(ignored)))
    def arm(address,label):
        if address not in breaks:
            breaks[address]=(mem(address,1),label); write(address,b'\xcc')
    def save_local_profile(force=False):
        nonlocal next_profile_save
        if not local_account or not log.get('local_profile_native_cache_returned'):return
        if not force and time.monotonic()<next_profile_save:return
        next_profile_save=time.monotonic()+2
        from codol_rebuild.client.v1649370.profile import view_state
        current_view=view_state(mem)
        if current_view!=log.get('local_view_state'):
            log['local_view_state']=current_view
            record({'local_view_state':current_view})
        from codol_rebuild.client.v1649370.profile import PUBLIC,SECURE,CAPACITY,STRIDE
        from codol_rebuild.accounts.bootstrap_service import IDENTITIES
        from codol_rebuild.profiles.fields import Fields
        blobs={k:mem(base+0x90,0x7FFC) for k,base in [('public',PUBLIC),('secure',SECURE)]}
        if any(mem(base+0x90,0x7FFC)!=blobs[k] for k,base in [('public',PUBLIC),('secure',SECURE)]):return
        for kind,identity in IDENTITIES.items():
            if blobs[kind][:8]!=struct.pack('<II',identity['version'],identity['checksum']):
                raise ValueError('Runtime profile schema changed; refusing save')
        fields=Fields(profile_schemas['public'])
        index=fields.read(blobs['public'],'currentCharacterIndex')
        character=fields.read(blobs['public'],f'characterClasses.{index}.slots.0')
        if character<=0:raise ValueError('Runtime selected character missing; refusing save')
        pool=int(log['local_inventory']['pool'],16)
        if mem(pool-16,16)!=b'LOCAL-INVENTORY!' or mem(pool+CAPACITY*STRIDE,16)!=b'LOCAL-INVENTORY!':
            raise ValueError('Inventory boundary changed')
        if blobs==local_account['profiles']:return
        local_account['revision']=profile_repository.save('local',local_account['revision'],blobs,IDENTITIES)
        local_account['profiles']=blobs
        log['local_profile_saved']={'revision':local_account['revision'],'character':character,'time':time.time()}
        record({'local_profile_saved':log['local_profile_saved']})
    def context(tid):
        thread=check(api(K,'OpenThread',PTR,W.DWORD,W.BOOL,W.DWORD)(26,False,tid))
        ctx=C.create_string_buffer(716); struct.pack_into('<I',ctx,0,0x1003f)
        try: check(api(K,'Wow64GetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
        except Exception: close(thread); raise
        return thread,ctx
    def cstring(address,wide=False,strict=False):
        if not address: return ''
        data=bytearray(); step=2 if wide else 1
        for index in range(2048):
            part=mem(address+index*step,step)
            if part==bytes(step): break
            data.extend(part)
        else:
            if strict:raise ValueError('字符串超过读取上限且未遇到终止符')
        return data.decode('utf-16-le' if wide else 'gbk',errors='replace')
    def install_file_trace():
        # 在 CRT 入口安装，此时目标 IAT 已由 WOW64 加载器解析。
        if __package__:
            from codol_rebuild.runtime.file_trace import build_file_gate
        else:
            from codol_rebuild.runtime.file_trace import build_file_gate
        imports={s.name.decode():s.address for d in pe.DIRECTORY_ENTRY_IMPORT for s in d.imports if s.name}
        get_error=struct.unpack('<I',mem(imports['GetLastError'],4))[0]
        set_error=struct.unpack('<I',mem(imports['SetLastError'],4))[0]
        for name in ('CreateFileA','CreateFileW'):
            slot=imports[name];original=struct.unpack('<I',mem(slot,4))[0]
            entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            code,trap=build_file_gate(entry,original,get_error,set_error,wide=name.endswith('W'))
            write(entry,code);old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,entry,4096,32,C.byref(old)))
            write(slot,struct.pack('<I',entry));file_traps[trap]=name
            record({'file_trace_installed':name,'iat':hex(slot),'original':hex(original),'entry':hex(entry),'trap':hex(trap)})
    def install_menu_preview():
        nonlocal name_adapter
        if __package__:
            from codol_rebuild.client.v1649370.menu_preview import build_menu_preview_helper,validate_menu_preview_image
        else:
            sys.path.insert(0,str(ROOT/'src'))
            from codol_rebuild.client.v1649370.menu_preview import build_menu_preview_helper,validate_menu_preview_image
        validate_menu_preview_image(image.read_bytes())
        entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
        initializer=None
        if args.local_profile:
            from codol_rebuild.client.v1649370 import ui_movie
            if mem(ui_movie.LOAD_SITE,len(ui_movie.LOAD_BYTES)) != ui_movie.LOAD_BYTES:
                raise ValueError('大厅 UI 加载调用边不匹配')
            arm(ui_movie.LOAD_SITE,'Local navigation resource')
            from codol_rebuild.client.v1649370.profile import local_optional_service_patches,local_welcome_page_patch
            welcome_patch=local_welcome_page_patch()
            local_patches=local_optional_service_patches()+[welcome_patch]
            for va,before,after in local_patches:
                if mem(va,len(before))!=before:
                    raise ValueError(f'Local callback mismatch at {va:#x}')
            for va,before,after in local_patches:
                write(va,after)
                if mem(va,len(after))!=after:
                    raise ValueError(f'Local callback readback mismatch at {va:#x}')
            record({'local_unsupported_services':['WarGod credit rating'],
                    'behavior':'void request ignored; no score fabricated'})
            record({'local_welcome_auto_popup_disabled':True,
                    'callback':'IsWelcomePageShowed','patch_va':hex(welcome_patch[0]),
                    'behavior':'report welcome already shown; manual MOTD entry unchanged'})
            from codol_rebuild.client.v1649370.profile import VIEW_TRACE_POINTS,VIEW_RESULT_POINTS
            for address,label in (VIEW_TRACE_POINTS | VIEW_RESULT_POINTS).items():arm(address,label)
            from codol_rebuild.client.v1649370.profile import build_initializer,inventory_relocations,CAPACITY,STRIDE
            initializer=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            profile_data=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            pool_allocation=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,CAPACITY*STRIDE+32,0x3000,4))
            pool=pool_allocation+16
            write(pool_allocation,b'LOCAL-INVENTORY!')
            write(pool+CAPACITY*STRIDE,b'LOCAL-INVENTORY!')
            relocations=inventory_relocations(pool)
            for va,before,after in relocations:
                if mem(va,len(before))!=before:raise ValueError(f'Inventory operand mismatch at {va:#x}')
            for va,before,after in relocations:write(va,after)
            bridge=build_initializer(initializer,profile_data,pool)
            write(initializer,bridge['code']);write(profile_data,bridge['data'])
            from codol_rebuild.client.v1649370.profile import build_stats_ready_adapter
            stats_entry=initializer+3072
            stats=build_stats_ready_adapter(stats_entry,profile_data)
            if len(bridge['code'])>3072 or mem(stats['patch_va'],5)!=stats['before']:
                raise ValueError('Local UI stats adapter image/region mismatch')
            write(stats_entry,stats['code'])
            write(stats['patch_va'],stats['after'])
            if mem(stats_entry,len(stats['code']))!=stats['code'] or mem(stats['patch_va'],5)!=stats['after']:
                raise ValueError('Local UI stats adapter readback mismatch')
            from codol_rebuild.client.v1649370.viewer import build_local_viewer_adapter
            viewer_entry=initializer+3328
            viewer=build_local_viewer_adapter(viewer_entry,profile_data+72)
            if len(stats['code'])>256 or len(viewer['code'])>768 or mem(viewer['patch_va'],len(viewer['gate_before']))!=viewer['gate_before']:
                raise ValueError('Local viewer adapter image/region mismatch')
            write(viewer_entry,viewer['code']);write(viewer['patch_va'],viewer['after'])
            if mem(viewer_entry,len(viewer['code']))!=viewer['code'] or mem(viewer['patch_va'],5)!=viewer['after']:
                raise ValueError('Local viewer adapter readback mismatch')
            log['local_viewer_adapter']={'patch_va':hex(viewer['patch_va']),'local_modes':viewer['local_modes'],
                'body_va':hex(viewer['body_va']),'exit_va':hex(viewer['exit_va'])}
            record({'local_viewer_adapter':log['local_viewer_adapter']})
            from codol_rebuild.client.v1649370.profile import build_local_playlist_adapter,validate_local_playlist_contract
            validate_local_playlist_contract(mem)
            playlist_entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            playlist=build_local_playlist_adapter(playlist_entry,profile_data+72)
            if len(playlist['code'])>4096:
                raise ValueError('Local playlist adapter exceeds allocation')
            write(playlist_entry,playlist['code'])
            if mem(playlist_entry,len(playlist['code']))!=playlist['code']:
                raise ValueError('Local playlist adapter readback mismatch')
            old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,playlist_entry,4096,32,C.byref(old)))
            for patch in playlist['patches']:
                write(patch['va'],patch['after'])
                if mem(patch['va'],len(patch['after']))!=patch['after']:
                    raise ValueError('Local playlist patch readback mismatch')
            log['local_playlist_adapter']={'entry':hex(playlist_entry),
                'patches':[hex(p['va']) for p in playlist['patches']],
                'policy':'local ready offline, no season and zero configured playlist: UI ID -1; local -1 is invalid'}
            record({'local_playlist_adapter':log['local_playlist_adapter']})
            from codol_rebuild.client.v1649370.room_name import build_room_name_adapter,validate_room_name_contract
            validate_room_name_contract(mem)
            room_name_entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            room_name=build_room_name_adapter(room_name_entry,profile_data+72)
            write(room_name_entry,room_name['code'])
            if mem(room_name_entry,len(room_name['code']))!=room_name['code']:
                raise ValueError('Room name adapter readback mismatch')
            old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,room_name_entry,4096,32,C.byref(old)))
            write(room_name['patch_va'],room_name['after'])
            if mem(room_name['patch_va'],5)!=room_name['after']:
                raise ValueError('Room name call edge readback mismatch')
            record({'local_room_name_adapter':{'entry':hex(room_name_entry),
                'patch_va':hex(room_name['patch_va']),'policy':'local room names only; nonempty, <=255 UTF-8 bytes, no ASCII controls',
                'completion':'original cppRefreshTask; original UI task update'}})
            from codol_rebuild.client.v1649370.room import build_room_adapter,validate_room_contract
            validate_room_contract(mem)
            room_entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            room_state=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            room=build_room_adapter(room_entry,room_state,profile_data+72)
            if len(room['code'])>4096:
                raise ValueError('Local room adapter exceeds allocation')
            write(room_entry,room['code'])
            if mem(room_entry,len(room['code']))!=room['code']:
                raise ValueError('Local room adapter readback mismatch')
            old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,room_entry,4096,32,C.byref(old)))
            for patch in room['patches']:
                write(patch['va'],patch['after'])
                if mem(patch['va'],len(patch['after']))!=patch['after']:
                    raise ValueError('Local room patch readback mismatch')
            record({'local_room_adapter':{'entry':hex(room_entry),'state':hex(room_state),
                'patches':[hex(p['va']) for p in room['patches']],
                'completion':'original native session/create callbacks; local room ownership only'}})
            old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,initializer,4096,32,C.byref(old)))
            profile_traps[bridge['trap']]=(profile_data,'prepare',pool)
            profile_traps[bridge['after']]=(profile_data,'after',pool)
            record({'local_profile_bridge':hex(initializer),'trap':hex(bridge['trap'])})
            from codol_rebuild.client.v1649370 import player_name
            player_name.validate_contract(mem)
            name_entry=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            name_state=check(api(K,'VirtualAllocEx',PTR,PTR,PTR,SIZE,W.DWORD,W.DWORD)(pi.process,None,4096,0x3000,4))
            name_adapter=player_name.build_adapter(name_entry,name_state,profile_data+72,initializer)
            if len(name_adapter['code'])>4096:raise ValueError('昵称适配超出代码页')
            write(name_entry,name_adapter['code'])
            if mem(name_entry,len(name_adapter['code']))!=name_adapter['code']:
                raise ValueError('昵称适配代码回读失败')
            old=W.DWORD()
            check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,name_entry,4096,32,C.byref(old)))
            write(name_adapter['patch_va'],name_adapter['after'])
            if mem(name_adapter['patch_va'],4)!=name_adapter['after']:
                raise ValueError('昵称GFx回调安装回读失败')
            initializer=name_adapter['initializer']
            record({'local_player_name_adapter':{'entry':hex(name_entry),'state':hex(name_state),
                    'policy':'local offline only; free rename; durable commit before success; restore on startup'}})
        plan=build_menu_preview_helper(entry,initializer_va=initializer,
            initializer_ready_va=profile_data+72 if args.local_profile else None);code=plan['entry_bytes']
        if len(code)>4096 or mem(plan['patch_va'],len(plan['patch_before']))!=plan['patch_before']:raise ValueError('离线UI helper入口或原始调用不符')
        if mem(layout.CONNECTING_BRANCH,2)!=bytes.fromhex('7404') or mem(layout.CONNECTING_FALSE,2)!=bytes.fromhex('32c0'):raise ValueError('原生连接提示false分支字节不符')
        write(entry,code)
        if mem(entry,len(code))!=code:raise ValueError('离线UI helper代码回读不符')
        old=W.DWORD()
        check(api(K,'VirtualProtectEx',W.BOOL,PTR,PTR,SIZE,W.DWORD,C.POINTER(W.DWORD))(pi.process,entry,4096,32,C.byref(old)))
        write(plan['patch_va'],plan['patch_after']);write(layout.CONNECTING_BRANCH,bytes.fromhex('eb04'))
        if mem(plan['patch_va'],len(plan['patch_after']))!=plan['patch_after'] or mem(layout.CONNECTING_BRANCH,2)!=bytes.fromhex('eb04'):raise ValueError('离线UI试验补丁回读失败')
        for phase in ('before','after'):preview_traps[entry+plan[phase+'_trap_offset']]=phase
        preview_metadata.update(plan.get('metadata',{}))
        record({'offline_menu_preview_installed':True,'helper':hex(entry),'helper_sha256':hashlib.sha256(code).hexdigest(),
                'call_va':hex(plan['patch_va']),'call_before':plan['patch_before'].hex(),'call_after':plan['patch_after'].hex(),
                'connecting_branch':{'va':'0x60c6f7','before':'7404','after':'eb04'},'metadata':preview_metadata})
        (run_dir/'offline-menu-preview-helper.bin').write_bytes(code)
        log['offline_menu_preview']=True
    def observe_state():
        pointer=struct.unpack('<I',mem(layout.DW_DVAR,4))[0]
        display=struct.unpack('<I',mem(layout.DISPLAY_MODE,4))[0]
        slot=struct.unpack('<I',mem(layout.CLIENT_SLOT,4))[0]
        encoded=struct.unpack('<I',mem(layout.CLIENT_POINTERS+slot*4,4))[0] if slot<8 else None
        decoded=encoded^layout.CLIENT_POINTER_XOR if encoded else None
        allocated=struct.unpack('<I',mem(layout.CLIENT_ALLOCATED,4))[0]
        ready=False
        if allocated==1 and decoded and 0x10000<=decoded<=0xffe6c7df:
            try:mem(decoded+0xc7210,4);ready=True
            except OSError:pass
        return {'ui_resource_count':struct.unpack('<I',mem(layout.UI_RESOURCE_COUNT,4))[0],
                'dw_dvar_pointer':hex(pointer),'dw_enabled':mem(pointer+12,1)[0] if pointer else None,
                'display_mode_pointer':hex(display),'display_mode':struct.unpack('<I',mem(display+12,4))[0] if display else None,
                'client_object_slot':slot,'client_object_encoded':hex(encoded) if encoded is not None else None,
                'client_object_decoded':hex(decoded) if decoded is not None else None,
                'allocation_complete':allocated,'client_object_ready':ready}
    def enumerate_regions():
        """只读枚举目标已提交、可读的内存区，供 schema 指纹扫描；不改动任何页。"""
        regions=[]; address=0x10000
        while address<0x100000000:
            mbi=MBI()
            if not vquery(pi.process,C.c_void_p(address),C.byref(mbi),C.sizeof(mbi)): break
            base=mbi.BaseAddress or 0; size=mbi.RegionSize
            if size==0:
                address+=0x10000; continue
            # MEM_COMMIT=0x1000；跳过 PAGE_NOACCESS(0x01) 与 PAGE_GUARD(0x100)。
            if mbi.State==0x1000 and mbi.Protect and not (mbi.Protect & 0x101):
                regions.append((base,size))
            address=base+size if base+size>address else address+size
        return regions
    def install_navigation_resource():
        """在原版主电影解析前安装已校验的会话内资源补丁，并回读数据及长度。"""
        from codol_rebuild.client.v1649370.ui_movie import find_resource_patch
        plan=find_resource_patch(mem,enumerate_regions())
        if mem(plan['header_address'],16)!=plan['header_before']:
            raise ValueError('大厅资源头在安装前改变')
        write(plan['data_address'],plan['data_after'])
        if mem(plan['data_address'],len(plan['data_after']))!=plan['data_after']:
            raise ValueError('大厅资源补丁回读失败')
        write(plan['length_address'],plan['length_after'])
        if mem(plan['length_address'],4)!=plan['length_after']:
            raise ValueError('大厅资源长度回读失败')
        return {'installed':True,'header':hex(plan['header_address']),
                'compressed_size':len(plan['data_after']),
                'patched_sha256':plan['patched_sha256'],
                'patched_methods':['MatchMakingStripView.onGetPlayerWarGodData',
                                   'SanitizeController.sanitize',
                                   'WeaponLoadOutController.onSetLoadoutNameHandle',
                                   'MarketController.handleRedeemChangeName',
                                   'MarketController.onRedeemChangeNameComplete',
                                   'PlayerObj.getObj']}
    def capture_schema_scan():
        """在暂停点扫描 version+checksum 指纹，命中已重定位定义对象才导出结构。"""
        if __package__:
            from codol_rebuild.profiles import scan as schema_scan
            from codol_rebuild.profiles.schema import capture_schema
        else:
            sys.path.insert(0,str(ROOT/'src'))
            from codol_rebuild.profiles import scan as schema_scan
            from codol_rebuild.profiles.schema import capture_schema
        def safe_read(addr,size):
            try: return mem(addr,size)
            except ValueError as exc: raise OSError(str(exc))
        regions=enumerate_regions()
        matches=schema_scan.find_fingerprints(safe_read,regions,schema_scan.KNOWN_DEFINITIONS)
        result={'regions':len(regions),'scanned_bytes':sum(size for _,size in regions),'definitions':{}}
        for label,addresses in matches.items():
            chosen=schema_scan.select_definition(safe_read,addresses)
            entry={'matches':[hex(a) for a in addresses],
                   'selected':hex(chosen['selected']) if chosen['selected'] is not None else None,
                   'candidates':[{**cand,'address':hex(cand['address'])} for cand in chosen['candidates']]}
            if chosen['selected'] is not None:
                try:
                    entry['captured']=capture_schema(safe_read,chosen['selected'],run_dir/'schema'/label)
                except (OSError,ValueError) as exc:
                    entry['capture_error']=str(exc)
            result['definitions'][label]=entry
        return result
    try:
        image=args.image.resolve(); client=args.client.resolve()
        if not image.is_relative_to(ROOT/'workspaces'): raise ValueError('仅运行项目 workspaces 内候选')
        exe=stage/'codol1649370-offline.exe'
        shutil.copyfile(image,exe)
        log['input_image']={'path':str(image),'sha256':digest(image)}
        for name in DEPENDENCIES:
            source=client/name
            if source.is_file(): shutil.copyfile(source,stage/name)
        # CEF 从 locales 子目录读取语言包；只复制原客户端自带的数据包。
        locales=[]
        for source in sorted((client/'locales').glob('*.pak')):
            if not source.is_file() or not source.resolve().is_relative_to(client):raise ValueError('CEF语言包来源越界')
            destination=stage/'locales'/source.name;destination.parent.mkdir(exist_ok=True)
            expected=digest(source);shutil.copyfile(source,destination)
            if digest(destination)!=expected:raise ValueError('CEF语言包副本校验失败')
            locales.append({'path':'locales/'+source.name,'size':source.stat().st_size,'sha256':expected})
        (run_dir/'cef-locales-manifest.json').write_text(json.dumps(locales,ensure_ascii=False,indent=2),encoding='utf-8')
        log['cef_locales']={'count':len(locales),'total_bytes':sum(item['size'] for item in locales)}
        (stage/'iips.ini').write_text('[IIPS]\nenable_iips=0\n',encoding='ascii')
        for folder in ['players','main','localappdata','appdata','temp']:(stage/folder).mkdir()
        shared_folders=set()
        if not args.supply_resources:
            if args.resource_library:shared_folders.add('main/images')
            if args.loose_library:shared_folders.update(('main/models','main/sound'))
        if args.overlay:
            overlay=args.overlay.resolve()
            if not overlay.is_relative_to(ROOT/'workspaces'):raise ValueError('overlay 必须位于项目 workspaces')
            for source in overlay.rglob('*'):
                if not source.is_file():continue
                relative=source.relative_to(overlay)
                if source.suffix.lower() in ('.exe','.dll','.sys') or source.name.lower() in FORBIDDEN:raise ValueError('资源overlay不可携带执行组件')
                if '/'.join(relative.parts[:2]).lower() in shared_folders:
                    shared_overlay[relative.as_posix().lower()]=source
                    continue
                destination=stage/relative; destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
        phase_done('private-runtime-files')
        for library_arg,library_kind in ((args.resource_library,'iwi'),(args.loose_library,'loose_startup_resources')):
            if library_arg is None:continue
            if library_kind=='iwi':
                if __package__:
                    from codol_rebuild.resources.offline_resources import canonical_iwi as canonical_resource
                else:
                    from codol_rebuild.resources.offline_resources import canonical_iwi as canonical_resource
            else:
                if __package__:
                    from codol_rebuild.resources.loose_library import canonical_loose_resource as canonical_resource
                else:
                    sys.path.insert(0,str(ROOT/'src'))
                    from codol_rebuild.resources.loose_library import canonical_loose_resource as canonical_resource
            library=library_arg.resolve()
            if not library.is_relative_to(ROOT/'workspaces'):raise ValueError('资源库必须在项目 workspaces 下')
            manifest_path=library/'manifest.json';manifest_bytes=manifest_path.read_bytes();manifest=json.loads(manifest_bytes)
            if (manifest.get('schema')!=1 or manifest.get('source_exe_sha256')!='36997674b18f2896de5e9d755686f6922af10708f355175714058d0a0172b537'
                    or manifest.get('source_resource_version')!=461 or manifest.get('old_version_resources_used') is not False
                    or manifest.get('summary',{}).get('completed') is not True):raise ValueError('资源库清单版本不符或尚未完成')
            if library_kind!='iwi' and manifest.get('library_kind')!=library_kind:raise ValueError('松散资源库类型不符')
            if not isinstance(manifest.get('issues'),list) or manifest['issues']:raise ValueError('资源库仍有未解决的冲突或校验错误，拒绝不完整批量启动')
            if not isinstance(manifest.get('resources'),list) or not manifest['resources']:raise ValueError('资源库清单没有有效条目')
            summary=manifest['summary'];count=len(manifest['resources'])
            count_fields=('total','processed','published','already_present','conflict','error','validated_output_bytes','expected_output_bytes')
            if any(type(summary.get(key)) is not int or summary[key]<0 for key in count_fields):raise ValueError('资源库汇总计数无效')
            if (summary.get('status')!='complete' or summary['total']!=count or summary['processed']!=count
                    or summary['published']+summary['already_present']!=count or summary['conflict']!=0 or summary['error']!=0):raise ValueError('资源库清单与批量完成计数不符')
            if any(not isinstance(entry,dict) or type(entry.get('size')) is not int or entry['size']<=0 for entry in manifest['resources']):raise ValueError('资源库文件记录无效')
            expected_bytes=sum(entry['size'] for entry in manifest['resources'])
            if summary['validated_output_bytes']!=expected_bytes or summary['expected_output_bytes']!=expected_bytes:raise ValueError('资源库清单与批量完成字节数不符')
            cache_audit={}
            cache=prepare_cache(ROOT/'workspaces'/'offline-resource-cache', library, manifest_bytes,
                                manifest['resources'], canonical_resource, protect_cache, audit=cache_audit, force_verify=getattr(args,'verify_resources',False))
            names=set();total=0;mounted=set()
            for entry in manifest['resources']:
                name=entry['game_path'];relative=Path(name)
                names.add(name);total+=entry['size']
                folder='/'.join(relative.parts[:2])
                if folder in shared_folders:
                    if name in shared_overlay:
                        overlay_source=shared_overlay.pop(name)
                        if digest(overlay_source)!=entry['sha256']:
                            raise ValueError('资源库与overlay内容冲突: '+name)
                    if folder not in mounted:
                        pending_mounts.append((cache/folder,stage/folder))
                        mounted.add(folder)
                    continue
                source=(cache/relative).resolve();destination=(stage/relative).resolve()
                if not source.is_relative_to(cache) or not destination.is_relative_to(stage):raise ValueError('资源库文件越界')
                destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists():
                    if destination.stat().st_size!=entry['size'] or digest(destination)!=entry['sha256']:raise ValueError('资源库与overlay内容冲突: '+name)
                elif destination.suffix.lower()=='.cfg':
                    shutil.copyfile(source,destination)
                else:
                    link_resource(source,destination)
            manifest_name='resource-library-manifest.json' if library_kind=='iwi' else 'loose-library-manifest.json'
            (run_dir/manifest_name).write_text(json.dumps({'schema':2,'storage':'shared-cache-reference',
                'manifest_path':str(cache/'manifest.json'),'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest()},indent=2),encoding='utf-8')
            log['resource_library' if library_kind=='iwi' else 'loose_library']={'path':str(library),'cache_path':str(cache),'storage':'directory-junctions' if mounted else 'hardlinks-with-private-config','validation':cache_audit,
                'manifest_sha256':hashlib.sha256(manifest_bytes).hexdigest(),'resource_count':len(names),'total_bytes':total}
            phase_done('resources-'+library_kind)
        if shared_overlay:raise ValueError('共享目录存在未登记的overlay资源: '+next(iter(shared_overlay)))
        from codol_rebuild.storage.game_settings import SettingsStore, SettingsSession
        settings_store=SettingsStore(ROOT/'workspaces/accounts/game-settings.sqlite3')
        settings_session=SettingsSession(settings_store,stage/'players')
        log['game_settings']={'database':str(settings_store.path),'restored_files':settings_session.restore()}
        raw=bytearray(exe.read_bytes()); pe=pefile.PE(data=raw)
        if pe.OPTIONAL_HEADER.ImageBase!=layout.IMAGE_BASE:raise ValueError('要求固定加载基址0x400000')
        if args.patches:
            log['patches']=json.loads(args.patches.read_text(encoding='utf-8'))
            for patch in log['patches']:
                address=int(patch['va'],0); before=bytes.fromhex(patch['before']); after=bytes.fromhex(patch['after'])
                offset=pe.get_offset_from_rva(address-layout.IMAGE_BASE)
                if len(before)!=len(after) or raw[offset:offset+len(before)]!=before:raise ValueError('补丁原字节不匹配')
                raw[offset:offset+len(before)]=after
            exe.write_bytes(raw)
        log['runtime_image_sha256']=digest(exe)
        log['arguments']=args.argument
        if args.supply_resources:
            if __package__:
                from codol_rebuild.resources.offline_resources import OfflineResourceSupplier
            else:
                from codol_rebuild.resources.offline_resources import OfflineResourceSupplier
            supplier=OfflineResourceSupplier(ROOT.parent,stage)
            if args.prewarm_run:
                previous=(allowed/args.prewarm_run).resolve()
                if previous.parent!=allowed or previous==run_dir:raise ValueError('预热来源必须是既有的同项目会话')
                records=[json.loads(line) for line in (previous/'resource-supply.jsonl').read_text(encoding='utf-8').splitlines()]
                names=sorted({row['logical_path'] for row in records if row.get('status') in ('supplied','already_present')})
                # 历史日志仅提供请求集合；资源仍由当前固定来源重新校验、解码和供应。
                for name in names:
                    supplied=supplier.supply(name)
                    if supplied.get('status') not in ('supplied','already_present'):raise ValueError('预热资源校验失败: '+json.dumps(supplied,ensure_ascii=False))
                log['prewarm']={'requests_from':str(previous),'verified_resource_count':len(names),'method':'revalidate current pinned sources before launch'}
        for options in [('/grant:r','*'+current_sid()+':(OI)(CI)F'),('/setintegritylevel','(OI)(CI)L')]:
            acl=subprocess.run(['icacls',str(stage),*options],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if acl.returncode:raise RuntimeError('独立运行目录ACL失败: '+(acl.stdout+acl.stderr).decode('mbcs',errors='replace'))
        # ACL 完成后才挂接缓存，防止运行目录权限传播进入共享资源。
        from codol_rebuild.platform.resource_mount import mount_resource
        for source,destination in pending_mounts:
            mount_resource(source,destination,ROOT/'workspaces/offline-resource-cache',stage)
        phase_done('settings-patches-acl-mounts')
        if getattr(args,'prepare_only',False):
            log['prepared_only']=True
            return log
        token=low_token(); si=SI(); si.cb=C.sizeof(si)
        display_defaults=[] if 'config_mp.cfg' in settings_session.baseline else ['+set','r_displayMode','windowed']
        log['command_line']=subprocess.list2cmdline([str(exe),'-q',*display_defaults,*args.argument])
        command=C.create_unicode_buffer(log['command_line'])
        env={key:os.environ[key] for key in ('SystemRoot','WINDIR','COMSPEC','SystemDrive','NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE') if key in os.environ}
        env.update({'PATH':str(stage)+';'+os.environ['SystemRoot']+'\\System32','TEMP':str(stage/'temp'),'TMP':str(stage/'temp'),'USERPROFILE':str(stage),'LOCALAPPDATA':str(stage/'localappdata'),'APPDATA':str(stage/'appdata')})
        environment=C.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items()))+'\0\0')
        check(api(A,'CreateProcessAsUserW',W.BOOL,PTR,W.LPCWSTR,W.LPWSTR,PTR,PTR,W.BOOL,W.DWORD,PTR,W.LPCWSTR,C.POINTER(SI),C.POINTER(PI))(token,str(exe),command,None,None,False,0x406,environment,str(stage),C.byref(si),C.byref(pi)))
        log['pid']=pi.pid; log['token']=token_info(pi.process)
        if log['token']!={'elevated':0,'appcontainer':0,'integrity':4096}:raise RuntimeError('目标令牌不符合低权限隔离约束')
        times=[C.c_uint64() for _ in range(4)]
        check(api(K,'GetProcessTimes',W.BOOL,PTR,*([PTR]*4))(pi.process,*(C.byref(t) for t in times)))
        log['creation_time']=times[0].value
        guard_path=ROOT/'bin'/'offline_guard.exe'
        log['guard_sha256']=digest(guard_path)
        request=SHELLINFO();request.size=C.sizeof(request);request.mask=320;request.verb='runas';request.file=str(guard_path)
        expected_bound=0 if args.until_close else max(120,args.seconds+60)
        request.parameters=subprocess.list2cmdline([str(run_dir),str(pi.pid),str(exe),str(times[0].value),str(expected_bound)]);request.show=0
        check(api(C.WinDLL('shell32',use_last_error=True),'ShellExecuteExW',W.BOOL,C.POINTER(SHELLINFO))(C.byref(request)))
        guard=request.process
        until=time.monotonic()+30
        while time.monotonic()<until and not (run_dir/'guard-ready.json').exists() and wait(guard,100)!=0:pass
        if not (run_dir/'guard-ready.json').exists():raise RuntimeError('断网守卫未确认ready；客户端保持挂起')
        while True:
            try:ready=json.loads((run_dir/'guard-ready.json').read_text());break
            except json.JSONDecodeError:
                if time.monotonic()>=until:raise
                time.sleep(.05)
        if ready['scope']!='offline-deny-all' or len(ready['filter_ids'])!=4 or not ready['job_kill_on_close'] or ready['max_processes']!=1 or ready['child_pid']!=pi.pid or int(ready['creation_filetime'])!=times[0].value or not ready['dynamic']:raise ValueError('断网守卫状态不符')
        if ready.get('bound_seconds')!=expected_bound:raise ValueError('断网守卫持续时限不符合本次运行模式')
        log['guard']=ready
        phase_done('process-and-uac-guard')
        check(api(K,'DebugSetProcessKillOnExit',W.BOOL,W.BOOL)(True))
        if api(K,'ResumeThread',W.DWORD,PTR)(pi.thread)==0xffffffff:raise C.WinError(C.get_last_error())
        log['resumed']=True;log['lifetime']='until_client_exit' if args.until_close else 'bounded_observation';until=time.monotonic()+args.seconds
        while args.until_close or time.monotonic()<until:
            save_game_settings()
            event=EVENT()
            if not api(K,'WaitForDebugEvent',W.BOOL,C.POINTER(EVENT),W.DWORD)(C.byref(event),200):
                if C.get_last_error()==121:
                    if args.local_profile:
                        try:save_local_profile()
                        except (OSError,ValueError) as error:log['local_profile_save_error']=str(error)
                    continue
                raise C.WinError(C.get_last_error())
            pending=event; data=bytes(event.data); disposition=0x10002; tids.add(event.tid)
            if event.code in (3,6):
                handle=struct.unpack_from('<Q',data)[0]; base=struct.unpack_from('<Q',data,24 if event.code==3 else 8)[0]
                path='unknown'
                if handle:
                    text=C.create_unicode_buffer(32768)
                    if api(K,'GetFinalPathNameByHandleW',W.DWORD,PTR,W.LPWSTR,W.DWORD,W.DWORD)(handle,text,len(text),0):path=text.value
                    close(handle)
                row={'module':path,'base':hex(base)};log['modules'].append(row);record(row)
                name=Path(path).name.lower()
                if name in FORBIDDEN:log['stop_reason']='forbidden_module_before_initialization';break
                if event.code==3:
                    if base!=layout.IMAGE_BASE:raise ValueError('实际ImageBase不符')
                    arm(layout.CRT_ENTRY,'CRT entry');arm(layout.WIN_MAIN,'WinMain')
                    if args.queue_main_menu:
                        arm(layout.ENGINE_RETURN,'Engine init returned');arm(layout.MAIN_MENU,'Switch to main menu')
                    if args.capture_schema:
                        # 晚期只读采样点：前端帧已加载 code_post_gfx_mp 的 DDL 资产。
                        arm(layout.FRONTEND_TICK,'Frontend tick');arm(layout.MAIN_MENU,'Switch to main menu')
                    for value in args.breakpoint:
                        address,label=value.split(':',1);arm(int(address,0),label)
                if name=='user32.dll':
                    local=Path(path.removeprefix('\\\\?\\'))
                    loaded=pefile.PE(str(local))
                    for symbol in loaded.DIRECTORY_ENTRY_EXPORT.symbols:
                        if symbol.name in (b'MessageBoxA',b'MessageBoxW') and not symbol.forwarder:arm(base+symbol.address,symbol.name.decode())
            elif event.code==1:
                code=struct.unpack_from('<I',data)[0]; address=struct.unpack_from('<Q',data,16)[0]; first=struct.unpack_from('<I',data,152)[0]
                row={'exception':hex(code),'address':hex(address),'first_chance':first,'tid':event.tid}
                parameters=[struct.unpack_from('<Q',data,32+i*8)[0] for i in range(min(struct.unpack_from('<I',data,24)[0],15))]
                row['parameters']=[hex(v) for v in parameters]
                if code==0x406d1388 and first and len(parameters)==4 and parameters[0]==0x1000 and parameters[3]==0:
                    # 微软约定的调试器线程命名通知，不是游戏错误。
                    # https://learn.microsoft.com/en-us/visualstudio/debugger/tips-for-debugging-threads
                    row['debugger_notification']='thread_name'
                    try:row['thread_name']=cstring(parameters[1])
                    except OSError:row['thread_name_unreadable']=True
                    record(row)
                elif (code in (0x80000003,0x4000001f) and name_adapter
                      and address in name_adapter['traps']):
                    from codol_rebuild.client.v1649370 import player_name
                    from codol_rebuild.accounts.bootstrap_service import validate_display_name,change_display_name
                    if event.tid!=pi.tid or observe_state()['dw_enabled']!=0:
                        raise ValueError('本地改名必须在离线主线程执行')
                    phase=name_adapter['traps'][address];state=name_adapter['state']
                    thread,ctx=context(event.tid)
                    try:
                        frame=struct.unpack_from('<I',ctx,180)[0]
                        if frame!=struct.unpack_from('<I',ctx,196)[0]:
                            raise ValueError('昵称桥保存帧不匹配')
                        row['local_player_name_phase']=phase
                        if phase=='request':
                            pending_name=None
                            try:
                                value=player_name.read_request(mem,frame,name_adapter['ready'])
                                validate_display_name(value)
                                player_name.stage_name(mem,write,state,value)
                                pending_name=value
                            except (OSError,ValueError) as error:
                                write(state,bytes(8))
                                row['local_player_name_error']=str(error)
                        elif phase=='applied':
                            try:
                                if pending_name is None or name_snapshot is None:
                                    raise ValueError('昵称请求没有对应宿主状态')
                                player_name.verify_applied(mem,state)
                                name_snapshot=change_display_name(profile_repository,name_snapshot['revision'],pending_name)
                            except (OSError,ValueError,sqlite3.Error) as error:
                                row['local_player_name_error']=str(error)
                                # 默认未提交；恢复原生旧名后才通过原任务回调失败。
                                write(state+4,bytes(4))
                            else:
                                write(state+4,struct.pack('<I',1))
                                if mem(state+4,4)!=struct.pack('<I',1):
                                    raise ValueError('昵称提交确认回读失败')
                                row['local_player_name_saved']=name_snapshot.copy()
                                pending_name=None
                        elif phase=='rolled_back':
                            player_name.verify_applied(mem,state,rollback=True)
                            pending_name=None
                            row['local_player_name_rolled_back']=True
                        elif phase=='restored':
                            player_name.verify_applied(mem,state)
                            write(state,bytes(8))
                            row['local_player_name_restored']=name_snapshot.copy()
                        else:
                            raise ValueError('未知昵称桥阶段')
                        struct.pack_into('<I',ctx,184,address+1)
                        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
                    finally:close(thread)
                    record(row)
                elif code in (0x80000003,0x4000001f) and address in profile_traps:
                    from codol_rebuild.client.v1649370.profile import capture
                    if event.tid!=pi.tid or observe_state()['dw_enabled']!=0:
                        raise ValueError('Local profile bridge requires offline main thread')
                    profile_data,phase,pool=profile_traps[address]
                    if phase=='prepare':
                        from codol_rebuild.storage.sqlite_accounts import AccountRepository
                        from codol_rebuild.accounts.bootstrap_service import bootstrap
                        from codol_rebuild.client.v1649370.profile import PUBLIC,SECURE,populate_inventory
                        captured=capture(mem,profile_data,run_dir/'local-profile',pool)
                        schemas={kind:json.loads(Path(captured['schemas'][label]['schema']).read_text(encoding='utf-8'))
                                 for kind,label in [('public','playerdata'),('secure','playersecuredata')]}
                        profile_repository=AccountRepository(ROOT/'workspaces/accounts/local.sqlite3')
                        account=bootstrap(profile_repository,schemas,captured['tables'],captured['catalog'])
                        local_account=account;profile_schemas=schemas
                        from codol_rebuild.accounts.bootstrap_service import load_display_name
                        from codol_rebuild.client.v1649370 import player_name
                        name_snapshot=load_display_name(profile_repository)
                        if name_snapshot['value'] is not None:
                            player_name.stage_name(mem,write,name_adapter['state'],name_snapshot['value'])
                        for kind,base in [('public',PUBLIC),('secure',SECURE)]:
                            blob=account['profiles'][kind]
                            write(base+0x90,blob)
                            if mem(base+0x90,len(blob))!=blob:raise ValueError('Local profile readback mismatch')
                        log['local_profile']={k:v for k,v in account.items() if k!='profiles'}
                        weapon_xp=max(int(r[1]) for r in captured['tables']['mp/coWeaponPrimaryRankTable.csv'] if r[0].isdigit())
                        log['local_inventory']=populate_inventory(mem,write,pool,captured['catalog'],weapon_xp)
                        write(profile_data+64,struct.pack('<I',log['local_inventory']['items']))
                        log['local_profile_capture']={'catalog_count':len(captured['catalog']),
                                                     'tables':{k:len(v) for k,v in captured['tables'].items()}}
                    else:
                        from codol_rebuild.client.v1649370.profile import CAPACITY,STRIDE
                        if mem(pool-16,16)!=b'LOCAL-INVENTORY!' or mem(pool+CAPACITY*STRIDE,16)!=b'LOCAL-INVENTORY!':
                            raise ValueError('Local inventory boundary changed')
                        native_count=struct.unpack('<I',mem(layout.INVENTORY_COUNT,4))[0]
                        if native_count!=log['local_inventory']['items']:raise ValueError('Native inventory count mismatch')
                        log['local_inventory']['native_count']=native_count
                        log['local_inventory']['canaries_intact']=True
                        from codol_rebuild.profiles.fields import Fields
                        fields=Fields(profile_schemas['public'])
                        selected=fields.read(local_account['profiles']['public'],'currentCharacterIndex')
                        expected_character=fields.read(local_account['profiles']['public'],f'characterClasses.{selected}.slots.0')
                        cached_character=struct.unpack('<I',mem(profile_data+68,4))[0]
                        log['local_inventory']['selected_character_cache']=cached_character
                        if cached_character!=expected_character or cached_character<=0:
                            raise ValueError('Native selected-character cache not initialized for inventory UI')
                        from codol_rebuild.client.v1649370.profile import publish_empty_local_store
                        log['local_store']=publish_empty_local_store(mem,write,native_count)
                        write(profile_data+72,struct.pack('<I',1))
                        if mem(profile_data+72,4)!=struct.pack('<I',1):
                            raise ValueError('Local UI stats readiness readback mismatch')
                        log['local_profile_ui_stats_ready']=True
                        record({'local_store':log['local_store']})
                        log['local_profile_native_cache_returned']=True
                    thread,ctx=context(event.tid)
                    try:
                        struct.pack_into('<I',ctx,184,address+1)
                        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
                    finally:close(thread)
                    record({'local_profile_phase':phase,'local_profile':log.get('local_profile')})
                elif code in (0x80000003,0x4000001f) and address in preview_traps:
                    phase=preview_traps[address];thread,ctx=context(event.tid)
                    try:
                        eax=struct.unpack_from('<I',ctx,176)[0]
                        esp=struct.unpack_from('<I',ctx,196)[0]
                        row['offline_menu_preview_phase']=phase;row['eax']=hex(eax);row['observed_state']=observe_state()
                        row['saved_getter_eax']=struct.unpack('<I',mem(esp+28,4))[0]
                        row['saved_caller_return']=hex(struct.unpack('<I',mem(esp+36,4))[0])
                        row['catalog']={'initialized':mem(layout.CATALOG_READY,1)[0],'count':struct.unpack('<I',mem(layout.CATALOG_COUNT,4))[0],
                                        'records':hex(struct.unpack('<I',mem(layout.CATALOG_RECORDS,4))[0])}
                        row['native_cpp_activate_called']=mem(layout.UI_ACTIVATED,1)[0]
                        row['root_gfx_value']={'type':struct.unpack('<I',mem(layout.ROOT_TYPE,4))[0],
                                               'interface':hex(struct.unpack('<I',mem(layout.ROOT_INTERFACE,4))[0]),
                                               'value':hex(struct.unpack('<I',mem(layout.ROOT_VALUE,4))[0])}
                        if event.tid!=pi.tid or row['observed_state']['dw_enabled']!=0:raise ValueError('离线UI激活必须发生于本次主线程且DW关闭')
                        if row['saved_getter_eax']!=1 or row['saved_caller_return']!='0x5ebff8':raise ValueError('离线UI helper调用栈或原getter结果不符')
                        if phase=='before':
                            if args.local_profile:
                                from codol_rebuild.client.v1649370.profile import view_state
                                row['local_view_state']=view_state(mem)
                                if (row['local_view_state']['viewer_initialized']!=1 or
                                        row['local_view_state']['weapon_definition_count']<=0):
                                    raise ValueError('Local UI activation preceded native weapon/viewer readiness')
                            if (row['catalog']['initialized']!=1 or row['catalog']['count']<=0 or row['catalog']['records']=='0x0'
                                    or row['native_cpp_activate_called']!=0 or not row['observed_state']['client_object_ready']):raise ValueError('离线UI激活真实本地对象尚未就绪')
                            row['activation_cycle']=preview_cycles.begin(row['native_cpp_activate_called'])
                            log['offline_menu_preview_activation_count']=preview_cycles.count
                            log['offline_menu_preview_attempted']=True
                        else:
                            preview_cycles.complete(eax&255,row['native_cpp_activate_called'])
                            row['activation_cycle']=preview_cycles.count
                            row['native_invoke_result']=eax&255
                            log['offline_menu_preview_returned']=True;log['offline_menu_preview_invoke_result']=eax&255
                        struct.pack_into('<I',ctx,184,address+1)
                        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
                    finally:close(thread)
                    record(row)
                elif code in (0x80000003,0x4000001f) and address in file_traps:
                    thread,ctx=context(event.tid)
                    try:
                        ebp=struct.unpack_from('<I',ctx,180)[0]
                        pointer=struct.unpack('<I',mem(ebp+8,4))[0]
                        row['file_missing']={'api':file_traps[address],'error':struct.unpack('<I',mem(ebp-8,4))[0],
                                             'caller':hex(struct.unpack('<I',mem(ebp+4,4))[0])}
                        try:
                            path=cstring(pointer,file_traps[address].endswith('W'),strict=True);row['file_missing']['path']=path
                            if supplier:
                                supplied=supplier.supply(path);row['resource_supply']=supplied
                                if supplied.get('status') in ('supplied','already_present'):
                                    write(ebp-12,struct.pack('<I',1))
                        except (OSError,ValueError) as error:row['file_missing']['error_detail']=str(error)
                        struct.pack_into('<I',ctx,184,address+1)
                        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
                    finally:close(thread)
                    record(row)
                elif code in (0x80000003,0x4000001f) and address in breaks:
                    original,label=breaks.pop(address);thread,ctx=context(event.tid)
                    try:
                        esp=struct.unpack_from('<I',ctx,196)[0]
                        row.update({'milestone':label,'stack':mem(esp,128).hex(),'registers':{n:hex(struct.unpack_from('<I',ctx,o)[0]) for n,o in [('eax',176),('ecx',172),('edx',168),('ebx',164),('esi',160),('edi',156),('ebp',180),('esp',196)]}})
                        if label in ('Fatal error','Com Error'):
                            row['argument_strings']={}
                            for index,pointer in enumerate(struct.unpack('<8I',mem(esp+4,32))):
                                if pointer<0x10000:continue
                                try:row['argument_strings'][str(index+1)]=cstring(pointer)
                                except OSError:pass
                        if label.startswith('view-result:'):
                            from codol_rebuild.client.v1649370.profile import view_result,view_state
                            row['local_view_state']=view_state(mem)
                            try:
                                row['view_result']=view_result(mem,struct.unpack_from('<I',ctx,180)[0])
                            except (OSError,ValueError) as error:row['view_result_error']=str(error)
                        if label.startswith('view:'):
                            from codol_rebuild.client.v1649370.profile import view_state
                            row['local_view_state']=view_state(mem)
                            if label!='view:UIViewerFrame':
                                try:
                                    params=struct.unpack('<I',mem(esp+4,4))[0]
                                    values,args_pointer,count=struct.unpack('<3I',mem(params+12,12))
                                    row['view_argument_count']=count
                                    if count<=8 and args_pointer:
                                        row['view_arguments']=[{'type':struct.unpack('<I',mem(args_pointer+i*32+12,4))[0],
                                                               'value':struct.unpack('<I',mem(args_pointer+i*32+16,4))[0]}
                                                              for i in range(count)]
                                except (OSError,ValueError) as error:row['view_arguments_error']=str(error)
                        if label in ('WinMain','Com Init'):
                            pointer=struct.unpack('<I',mem(esp+12,4))[0] if label=='WinMain' else layout.STARTUP_ARGUMENTS
                            row['startup_arguments']=cstring(pointer,strict=True)
                        if label in ('Frontend tick','Switch to main menu','Engine init returned','Menu command queue'):
                            row['observed_state']=observe_state()
                        if label=='Engine init returned':
                            log['engine_init_returned']=True
                            phase_done('engine-init')
                            if args.queue_main_menu:arm(layout.COMMAND_QUEUE,'Menu command queue')
                        if label=='Menu command queue':
                            if __package__:
                                from codol_rebuild.client.v1649370.menu_queue import build_queue_plan
                            else:
                                from codol_rebuild.client.v1649370.menu_queue import build_queue_plan
                            values=struct.unpack('<3I',mem(esp,12))
                            pointer,capacity,length=struct.unpack('<3I',mem(layout.COMMAND_DESCRIPTOR,12))
                            state={**row['observed_state'],'thread_id':event.tid,'main_thread_id':pi.tid,
                                   'return_address':values[0],'local_client_index':values[1],'controller_index':values[2],
                                   'engine_returned':log.get('engine_init_returned',False),'already_queued':log.get('menu_command_queued',False)}
                            try:
                                if pointer!=layout.COMMAND_BUFFER or capacity!=0x20000 or length>=capacity:raise ValueError('命令缓冲结构不符合固定版本')
                                critical=struct.unpack('<4I',mem(layout.COMMAND_LOCK,16))
                                row['command_lock']={'recursion_count':critical[2],'owning_thread':critical[3]}
                                if critical[3]!=event.tid or critical[2]<1:raise ValueError('命令临界区未由当前主线程持有')
                                plan=build_queue_plan(pointer,capacity,length,mem(pointer,length) if length else b'',state)
                            except ValueError as error:row['menu_queue_refused']=str(error)
                            else:
                                write(plan['payload_address'],plan['payload'])
                                if mem(plan['payload_address'],len(plan['payload']))!=plan['payload']:raise ValueError('命令数据回读失败')
                                write(plan['length_address'],plan['length_bytes'])
                                if mem(plan['length_address'],4)!=plan['length_bytes']:raise ValueError('命令长度回读失败')
                                log['menu_command_queued']=True
                                row['menu_queue']={'new_length':plan['new_length'],'payload_address':hex(plan['payload_address']),'command':'switchtomainmenu'}
                        if label=='CRT entry' and args.trace_files:install_file_trace()
                        if label=='CRT entry' and args.offline_menu_preview:install_menu_preview()
                        if label=='Local navigation resource':
                            row['local_navigation_resource']=install_navigation_resource()
                        if label.startswith('MessageBox'):
                            values=struct.unpack('<4I',mem(esp,16));row['text']=cstring(values[2],label.endswith('W'));row['caption']=cstring(values[3],label.endswith('W'))
                            if '错误' in row['caption'] or 'error' in row['caption'].lower():log['stop_reason']='fatal_dialog'
                        if args.capture_schema and not log.get('schema_captured') and label in ('Engine init returned','Frontend tick','Switch to main menu'):
                            try:
                                row['schema_capture']=capture_schema_scan()
                                if any(item.get('captured') for item in row['schema_capture']['definitions'].values()):log['schema_captured']=True
                            except OSError as error:row['schema_capture_error']=str(error)
                        write(address,original);struct.pack_into('<I',ctx,184,address)
                        check(api(K,'Wow64SetThreadContext',W.BOOL,PTR,PTR)(thread,ctx))
                    finally:close(thread)
                    record(row)
                    if log.get('stop_reason')=='fatal_dialog':break
                elif code in (0x80000003,0x4000001f):record(row)
                else:
                    try:
                        thread,ctx=context(event.tid)
                        try:
                            eip=struct.unpack_from('<I',ctx,184)[0];esp=struct.unpack_from('<I',ctx,196)[0]
                            row['registers']={n:hex(struct.unpack_from('<I',ctx,o)[0]) for n,o in [('eax',176),('ecx',172),('edx',168),('ebx',164),('esi',160),('edi',156),('ebp',180),('eip',184),('esp',196)]}
                            row['instruction']=mem(eip,32).hex()
                            for stack_size in (4096,1024,256,64):
                                try:row['stack']=mem(esp,stack_size).hex();break
                                except OSError:pass
                            row['frames']=[];frame=struct.unpack_from('<I',ctx,180)[0];seen=set()
                            for _ in range(20):
                                if not frame or frame in seen:break
                                seen.add(frame)
                                try:
                                    values=struct.unpack('<18I',mem(frame,72));entry={'ebp':hex(frame),'return':hex(values[1]),'arguments':[hex(v) for v in values[2:]],'strings':{}}
                                    for value in values[2:10]:
                                        try:
                                            string=cstring(value)
                                            if string and all(ch.isprintable() or ch in '\r\n\t' for ch in string):entry['strings'][hex(value)]=string
                                        except (OSError,ValueError):pass
                                    row['frames'].append(entry)
                                    if values[0]<=frame or values[0]-frame>0x100000:break
                                    frame=values[0]
                                except OSError:break
                            row['parameters']=[hex(struct.unpack_from('<Q',data,32+i*8)[0]) for i in range(min(struct.unpack_from('<I',data,24)[0],15))]
                        finally:close(thread)
                    except OSError as error:row['context_error']=str(error)
                    record(row);disposition=0x80010001
                    if not first:log['stop_reason']='unhandled_exception';break
            elif event.code==5:
                log['exit_code']=struct.unpack_from('<I',data)[0];record({'exit_code':log['exit_code']})
            elif event.code==8:
                address=struct.unpack_from('<Q',data)[0];wide=struct.unpack_from('<H',data,8)[0]
                try:record({'debug_string':cstring(address,bool(wide))})
                except OSError:pass
            check(api(K,'ContinueDebugEvent',W.BOOL,W.DWORD,W.DWORD,W.DWORD)(event.pid,event.tid,disposition));pending=None
            if event.code==5:break
        else:log['stop_reason']='observation_timeout'
    except Exception as error:
        log['error']=str(error);log['traceback']=traceback.format_exc()
    finally:
        if pi.process and local_account and not log.get('error') and log.get('stop_reason') not in ('fatal_dialog','unhandled_exception'):
            try:
                if wait(pi.process,0)==258:save_local_profile(force=True)
            except (OSError,ValueError) as error:log['local_profile_save_error']=str(error)
        if pi.process:
            if wait(pi.process,0)!=0:api(K,'TerminateProcess',W.BOOL,PTR,W.UINT)(pi.process,0xd1a6)
            if pending:api(K,'ContinueDebugEvent',W.BOOL,W.DWORD,W.DWORD,W.DWORD)(pending.pid,pending.tid,0x80010001)
            until=time.monotonic()+4
            while wait(pi.process,0)!=0 and time.monotonic()<until:
                event=EVENT()
                if api(K,'WaitForDebugEvent',W.BOOL,C.POINTER(EVENT),W.DWORD)(C.byref(event),100):api(K,'ContinueDebugEvent',W.BOOL,W.DWORD,W.DWORD,W.DWORD)(event.pid,event.tid,0x10002)
            log['child_terminated']=wait(pi.process,1000)==0
            if log['child_terminated']:save_game_settings(final=log.get('exit_code')==0)
            close(pi.thread);close(pi.process)
        if token:close(token)
        if guard:
            (run_dir/'guard-done.json').write_text('{}')
            if wait(guard,8000)!=0:log['guard_cleanup_timeout']=True
            if (run_dir/'guard-result.json').exists():log['guard_cleanup']=json.loads((run_dir/'guard-result.json').read_text())
            close(guard)
        log['finished_at']=time.time();log_file.close()
        (run_dir/'result.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
    return log


def main(argv=None):
    """显式单次命令，所有会话放项目目录；不会把未验收窗口判成成功。"""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',type=Path,default=ROOT/'workspaces/offline-launch/codol1649370-offline.exe')
    parser.add_argument('--client',type=Path,default=ROOT.parent/'Original Client')
    parser.add_argument('--name',required=True)
    parser.add_argument('--verify-resources',action='store_true',help='强制资源 SHA-256 全量复核')
    parser.add_argument('--prepare-only',action='store_true',help='仅准备运行目录并计时，不创建客户端或守卫')
    lifetime=parser.add_mutually_exclusive_group()
    lifetime.add_argument('--seconds',type=int,default=30)
    lifetime.add_argument('--until-close',action='store_true')
    parser.add_argument('--patches',type=Path)
    parser.add_argument('--overlay',type=Path)
    parser.add_argument('--resource-library',type=Path)
    parser.add_argument('--loose-library',type=Path)
    parser.add_argument('--trace-files',action='store_true')
    parser.add_argument('--supply-resources',action='store_true')
    parser.add_argument('--queue-main-menu',action='store_true')
    parser.add_argument('--offline-menu-preview',action='store_true')
    parser.add_argument('--local-profile',action='store_true',help='Experimental build-specific local profile bridge')
    parser.add_argument('--capture-schema',action='store_true',
                        help='在前端帧只读扫描 DDL 指纹并导出档案结构定义（不写目标进程）')
    parser.add_argument('--prewarm-run')
    parser.add_argument('--breakpoint',action='append',default=[])
    parser.add_argument('argument',nargs='*')
    args=parser.parse_args(argv)
    if args.local_profile:args.offline_menu_preview=True
    if args.prewarm_run:args.supply_resources=True
    if args.supply_resources:args.trace_files=True
    if not 1<=args.seconds<=600:parser.error('单次观测时长必须在1至600秒之间')
    result=run(args)
    print(json.dumps({k:v for k,v in result.items() if k not in ('events','modules','traceback','patches')},ensure_ascii=False,indent=2))
    cleanup=result.get('guard_cleanup',{})
    failed=(result.get('error') or result.get('stop_reason') in ('unhandled_exception','forbidden_module_before_initialization','fatal_dialog')
            or result.get('exit_code',0)!=0 or result.get('child_terminated') is False
            or result.get('guard_cleanup_timeout') or cleanup.get('error') or cleanup.get('engine_close_status',0)!=0)
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
