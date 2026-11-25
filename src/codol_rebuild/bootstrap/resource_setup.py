"""装配终版原包按需视图或显式展开库，复用程序目录并绑定原生设置。"""
import hashlib, json, shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT, USER_DATA_ROOT
from codol_rebuild.resources.runtime_cache import prepare_library
from codol_rebuild.platform.resource_cache_acl import protect_cache
from codol_rebuild.platform.resource_acl import apply_runtime_acl
from codol_rebuild.adapters.game.v1649370.image import prepare_runtime_image
FORBIDDEN = {'codomp_client_shipretailbase.dll', 'tersafe.dll', 'tenslx.dll', 'tenproxy.dll', 'gamerpcs.dll', 'tenrpcs.dll', 'ace-ats32.dll', 'ace-drv32.dll', 'vsddrvdll.dll', 'policyprobe.dll', 'tp3helper.exe', 'gbspy.dll', 'tcj.dll'}

def prepare_native_library(library, run_dir, log, force_verify):
    """读取挂载计划及根级依赖；仅显式复核时扫描完整资源库。"""
    from codol_rebuild.resources.native_library import (
        canonical_native_resource, manifest_path, read_manifest, MOUNT_DIRECTORIES,
    )
    library = library.resolve()
    if not library.is_relative_to((ROOT / 'resources/v1649370').resolve()):
        raise ValueError('完整原生库必须位于项目固定版本资源目录')
    manifest_bytes, manifest = read_manifest(library, verify_resources=force_verify)
    validation_audit = {'validation': 'not-requested', 'hashed_bytes': 0, 'copied_bytes': 0}
    if force_verify:
        prepare_library(ROOT / 'workspaces/resource-library-verification', library,
                        manifest_bytes, manifest['resources'], canonical_native_resource, protect_cache,
                        audit=validation_audit, force_verify=True, external_manifest=manifest_path(library))
    reference = {'manifest_path': str(manifest_path(library)),
                 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                 'resource_root': str(library), 'storage': 'top-level-directory-junctions',
                 'mounted_directories': list(MOUNT_DIRECTORIES), 'validation': validation_audit,
                 'resource_count': len(manifest['resources']),
                 'total_bytes': manifest['summary']['validated_output_bytes']}
    (run_dir / 'native-library-reference.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
    log['native_library'] = reference
    return ([library / directory for directory in MOUNT_DIRECTORIES],
            [entry for entry in manifest['resources'] if '/' not in entry['game_path']])

def digest(path):
    """计算输入或候选的文件指纹。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def close_runtime_resources(runtime_lease, packed_view, log):
    """游戏停止后先断开本次资源连接并关闭读取器，再释放槽位租约。"""
    try:
        if packed_view is not None:
            packed_view.close()
            log['packed_library'].update(
                successful_fetches=packed_view.successful_fetches,
                delivered_bytes=packed_view.delivered_bytes,
                failed_fetches=packed_view.failed_fetches,
                view_closed=True,
            )
    except (OSError, RuntimeError, ValueError) as error:
        log['packed_view_cleanup_error'] = str(error)
    finally:
        if runtime_lease is not None:
            runtime_lease.close()


def prepare_reusable_runtime(args, run_dir, session_stage, log, phase_done, image):
    """复用按版本独占的完整程序目录；可写数据始终保存在当前会话中。"""
    from codol_rebuild.platform.runtime_directory import acquire_runtime_directory, SESSION_DIRECTORIES
    from codol_rebuild.platform.resource_mount import mount_resource
    from codol_rebuild.resources.runtime_cache import save_receipt
    verify_resources = getattr(args, 'verify_resources', False)
    packed_index = None
    if args.native_library is None:
        from codol_rebuild.resources.packed_library import read_packed_index
        library = ROOT / 'resources/v1649370/packed'
        packed_index = read_packed_index(library, verify_resources=verify_resources)
        dependencies = [member for path, member in packed_index.members.items() if '/' not in path]
        mount_sources = [(library, library / name) for name in ('locales', 'miles', 'Microsoft.VC90.CRT')]
        manifest_digest = packed_index.sha256
    else:
        library = args.native_library.resolve()
        resource_directories, dependencies = prepare_native_library(library, run_dir, log, verify_resources)
        mount_sources = [(library, directory) for directory in resource_directories]
        manifest_digest = log['native_library']['manifest_sha256']
    image_digest = digest(image)
    patch_bytes = args.patches.read_bytes() if args.patches else b'[]'
    revision_inputs = {'image_sha256': image_digest,
                       'patches_sha256': hashlib.sha256(patch_bytes).hexdigest(),
                       'library': str(library),
                       'manifest_sha256': manifest_digest}
    revision = hashlib.sha256(json.dumps(revision_inputs, sort_keys=True).encode('utf-8')).hexdigest()
    lease = acquire_runtime_directory(ROOT / 'workspaces/runtime-programs', revision,
                                      ROOT / 'workspaces/offline-runs')
    packed_view = None
    try:
        if packed_index is not None:
            from codol_rebuild.platform.cloud_files import CloudFilesView
            from codol_rebuild.resources.packed_view import start_packed_view
            # 版本及独占槽位确定同步根；新会话为同一根重新连接供件回调。
            view_root = lease.slot / 'packed-view'
            fetch_log = run_dir / 'packed-fetch.jsonl'
            log['packed_library'] = {
                'resource_root': str(library), 'manifest_sha256': manifest_digest,
                'view_root': str(view_root), 'fetch_log': str(fetch_log),
                'resource_count': len(packed_index.members),
                'verified_source_hashes': verify_resources,
            }
            packed_view = start_packed_view(library, packed_index.members, view_root,
                                            ROOT / 'bin/v1649370/packed-reader',
                                            audit_path=fetch_log, index_sha256=manifest_digest,
                                            view_factory=CloudFilesView)
            log['packed_library']['view_reused'] = packed_view.reused
            mount_sources.extend((view_root, view_root / name) for name in ('main', 'hires', 'client_shipretail'))
            phase_done('packed-resource-view')
        program = lease.path
        receipt_path = lease.slot / 'program.json'
        executable = program / 'codol1649370-offline.exe'
        log['input_image'] = {'path': str(image), 'sha256': image_digest}
        copied_bytes = 0
        if program.is_junction() or program.is_symlink():
            raise ValueError('运行程序目录不可重定向')
        if not program.exists():
            # 槽位租用已独占。先在同卷临时目录准备全部内容，最后一次改名发布。
            # 写入收据至改名之间即使中断，下次仍从空的最终位置重新安装。
            with TemporaryDirectory(prefix='install-', dir=lease.slot) as temporary_directory:
                staged_program = Path(temporary_directory) / 'runtime'
                staged_program.mkdir()
                staged_executable = staged_program / executable.name
                shutil.copyfile(image, staged_executable)
                copied_bytes += image.stat().st_size
                for entry in dependencies:
                    destination = staged_program / entry['game_path']
                    shutil.copyfile(library / entry['game_path'], destination)
                    if digest(destination) != entry['sha256']:
                        raise ValueError('运行依赖副本摘要不符: ' + entry['game_path'])
                    copied_bytes += entry['size']
                settings_file = staged_program / 'iips.ini'
                settings_file.write_text('[IIPS]\nenable_iips=0\n', encoding='ascii')
                prepared_image = prepare_runtime_image(staged_executable, args.patches, log)
                program_files = [*dependencies,
                                 {'game_path': executable.name, 'sha256': digest(staged_executable),
                                  'size': staged_executable.stat().st_size},
                                 {'game_path': 'iips.ini', 'sha256': digest(settings_file),
                                  'size': settings_file.stat().st_size}]
                # 先保护实体文件，再建立联接，避免递归修改联接目标权限。
                protect_cache(staged_program)
                for source_root, source in mount_sources:
                    mount_resource(source, staged_program / source.name, source_root, staged_program)
                save_receipt(receipt_path, {'inputs': revision_inputs, 'files': program_files})
                staged_program.rename(program)
            reused = False
        else:
            if not receipt_path.is_file():
                raise ValueError('运行目录缺少安装收据: ' + str(lease.slot))
            receipt = json.loads(receipt_path.read_bytes())
            if receipt['inputs'] != revision_inputs:
                raise ValueError('运行程序版本记录不符')
            expected_names = {entry['game_path'] for entry in dependencies} | {executable.name, 'iips.ini'}
            program_files = receipt['files']
            if {entry['game_path'] for entry in program_files} != expected_names:
                raise ValueError('运行程序文件清单不符')
            for entry in program_files:
                path = program / entry['game_path']
                if path.resolve().parent != program or path.is_symlink():
                    raise ValueError('运行程序文件不可重定向')
                if path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
                    raise ValueError('已准备程序文件发生变化: ' + str(path))
            for source_root, source in mount_sources:
                installed = program / source.name
                if not installed.is_junction() or installed.resolve() != source.resolve():
                    raise ValueError('运行资源联接发生变化: ' + str(installed))
            prepared_image = prepare_runtime_image(executable, None, log)
            log['patches'] = json.loads(patch_bytes)
            reused = True
        log['runtime_program'] = {'path': str(program), 'revision': revision,
                                  'reused': reused, 'copied_bytes': copied_bytes,
                                  'patches_applied_this_run': not reused}
        log['runtime_image_sha256'] = next(entry['sha256'] for entry in program_files
                                           if entry['game_path'] == executable.name)
        log['arguments'] = args.argument
        phase_done('runtime-program-reuse' if reused else 'runtime-program-install')
        for name in SESSION_DIRECTORIES:
            (session_stage / name).mkdir()
        players_directory = USER_DATA_ROOT / 'players'
        players_directory.mkdir(parents=True, exist_ok=True)
        apply_runtime_acl(players_directory)
        log['game_settings'] = {'directory': str(players_directory)}
        apply_runtime_acl(session_stage)
        lease.bind_session(session_stage)
        lease.bind_players(players_directory)
        phase_done('session-settings-and-mounts')
        return {'image': image, 'exe': executable, 'pe': prepared_image,
                'players_directory': players_directory, 'runtime_lease': lease, 'packed_view': packed_view}
    except BaseException:
        close_runtime_resources(lease, packed_view, log)
        raise

def prepare(args, run_dir, stage, log, phase_done):
    """校验固定镜像并按所选资源来源装配运行目录。"""
    image = args.image.resolve()
    if (image != (ROOT / 'bin/v1649370/codol1649370-offline.exe').resolve()
            and not image.is_relative_to((ROOT / 'workspaces').resolve())):
        raise ValueError('仅运行项目固定版本基底或 workspaces 内候选')
    return prepare_reusable_runtime(args, run_dir, stage, log, phase_done, image)
