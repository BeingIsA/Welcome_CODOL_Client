"""启动资源装配：可选完整资源复核、程序复用、设置恢复和顶层联接。"""
import hashlib, json, shutil
from pathlib import Path
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT, accounts_dir
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


def prepare_reusable_runtime(args, run_dir, session_stage, log, phase_done, image):
    """复用按版本独占的完整程序目录；可写数据始终保存在当前会话中。"""
    from codol_rebuild.platform.runtime_directory import acquire_runtime_directory, SESSION_DIRECTORIES
    from codol_rebuild.platform.resource_mount import mount_resource
    from codol_rebuild.resources.runtime_cache import save_receipt
    from codol_rebuild.adapters.persistence.game_settings import SettingsStore, SettingsSession
    library = args.native_library.resolve()
    resource_directories, dependencies = prepare_native_library(
        library, run_dir, log, getattr(args, 'verify_resources', False))
    image_digest = digest(image)
    patch_bytes = args.patches.read_bytes() if args.patches else b'[]'
    revision_inputs = {'image_sha256': image_digest,
                       'patches_sha256': hashlib.sha256(patch_bytes).hexdigest(),
                       'library': str(library),
                       'manifest_sha256': log['native_library']['manifest_sha256']}
    revision = hashlib.sha256(json.dumps(revision_inputs, sort_keys=True).encode('utf-8')).hexdigest()
    lease = acquire_runtime_directory(ROOT / 'workspaces/runtime-programs', revision,
                                      ROOT / 'workspaces/offline-runs')
    try:
        program = lease.path
        receipt_path = lease.slot / 'program.json'
        executable = program / 'codol1649370-offline.exe'
        log['input_image'] = {'path': str(image), 'sha256': image_digest}
        copied_bytes = 0
        if not receipt_path.exists():
            # 不把中断留下的不完整目录当成已准备版本，也不覆盖现场。
            if program.exists():
                raise ValueError('运行目录准备未完成，请检查后移走该槽位: ' + str(lease.slot))
            program.mkdir()
            shutil.copyfile(image, executable)
            copied_bytes += image.stat().st_size
            for entry in dependencies:
                destination = program / entry['game_path']
                shutil.copyfile(library / entry['game_path'], destination)
                if digest(destination) != entry['sha256']:
                    raise ValueError('运行依赖副本摘要不符: ' + entry['game_path'])
                copied_bytes += entry['size']
            (program / 'iips.ini').write_text('[IIPS]\nenable_iips=0\n', encoding='ascii')
            prepared_image = prepare_runtime_image(executable, args.patches, log)
            program_files = [*dependencies,
                             {'game_path': executable.name, 'sha256': digest(executable),
                              'size': executable.stat().st_size},
                             {'game_path': 'iips.ini', 'sha256': digest(program / 'iips.ini'),
                              'size': (program / 'iips.ini').stat().st_size}]
            # 必须在建立资源和会话联接前保护，避免递归修改联接目标权限。
            protect_cache(program)
            for source in resource_directories:
                mount_resource(source, program / source.name, library, program)
            save_receipt(receipt_path, {'inputs': revision_inputs, 'files': program_files})
            reused = False
        else:
            if program.is_junction() or program.is_symlink():
                raise ValueError('运行程序目录不可重定向')
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
            for source in resource_directories:
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
        settings_store = SettingsStore(accounts_dir() / 'game-settings.sqlite3')
        settings_session = SettingsSession(settings_store, session_stage / 'players')
        log['game_settings'] = {'database': str(settings_store.path),
                                'restored_files': settings_session.restore()}
        apply_runtime_acl(session_stage)
        lease.bind_session(session_stage)
        phase_done('session-settings-and-mounts')
        return {'image': image, 'exe': executable, 'pe': prepared_image,
                'settings': settings_session, 'runtime_lease': lease}
    except Exception:
        lease.close()
        raise

def prepare(args, run_dir, stage, log, phase_done):
    """校验镜像来源并装配唯一的完整资源启动流程。"""
    image = args.image.resolve()
    if (image != (ROOT / 'bin/v1649370/codol1649370-offline.exe').resolve()
            and not image.is_relative_to((ROOT / 'workspaces').resolve())):
        raise ValueError('仅运行项目固定版本基底或 workspaces 内候选')
    return prepare_reusable_runtime(args, run_dir, stage, log, phase_done, image)
