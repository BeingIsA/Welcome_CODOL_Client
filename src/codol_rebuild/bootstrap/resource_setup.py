"""离线资源装配：独立运行文件、缓存、设置恢复、ACL 与挂载顺序。"""
import hashlib, json, shutil
from pathlib import Path
from codol_rebuild.core.paths import REBUILD_ROOT as ROOT, accounts_dir
from codol_rebuild.resources.runtime_cache import prepare_cache, prepare_library, link_resource
from codol_rebuild.resources.library_manifest import validate_library_manifest
from codol_rebuild.platform.resource_cache_acl import protect_cache
from codol_rebuild.platform.resource_acl import apply_runtime_acl
from codol_rebuild.adapters.game.v1649370.image import prepare_runtime_image
FORBIDDEN = {'codomp_client_shipretailbase.dll', 'tersafe.dll', 'tenslx.dll', 'tenproxy.dll', 'gamerpcs.dll', 'tenrpcs.dll', 'ace-ats32.dll', 'ace-drv32.dll', 'vsddrvdll.dll', 'policyprobe.dll', 'tp3helper.exe', 'gbspy.dll', 'tcj.dll'}
DEPENDENCIES = ('GCloudVoice.dll', 'bink2w32.dll', 'dbghelp.dll', 'libcef.dll', 'mss32.dll', 'D3DX9_43.dll', 'd3dcompiler_43.dll', 'libEGL.dll', 'libGLESv2.dll', 'icudtl.dat', 'natives_blob.bin', 'snapshot_blob.bin', 'cef.pak', 'cef_100_percent.pak', 'cef_200_percent.pak', 'cef_extensions.pak', 'localization.txt', 'logo.bmp', 'splash.bmp')

def prepare_native_library(library, stage, run_dir, log, force_verify):
    """校验完整库，复制根级运行依赖；仅返回顶层挂载计划，ACL 完成后执行。"""
    from codol_rebuild.resources.native_library import (
        canonical_native_resource, manifest_path, read_manifest, MOUNT_DIRECTORIES,
    )
    library = library.resolve()
    if not library.is_relative_to((ROOT / 'resources/v1649370').resolve()):
        raise ValueError('完整原生库必须位于项目固定版本资源目录')
    manifest_bytes, manifest = read_manifest(library)
    validation_audit = {}
    prepare_library(ROOT / 'workspaces/resource-library-verification', library,
                    manifest_bytes, manifest['resources'], canonical_native_resource, protect_cache,
                    audit=validation_audit, force_verify=force_verify, external_manifest=manifest_path(library))
    for entry in manifest['resources']:
        if '/' not in entry['game_path']:
            destination = stage / entry['game_path']
            shutil.copyfile(library / entry['game_path'], destination)
            if digest(destination) != entry['sha256']:
                raise ValueError('运行依赖副本摘要不符: ' + entry['game_path'])
    reference = {'manifest_path': str(manifest_path(library)),
                 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                 'resource_root': str(library), 'storage': 'top-level-directory-junctions',
                 'mounted_directories': list(MOUNT_DIRECTORIES), 'validation': validation_audit,
                 'resource_count': len(manifest['resources']),
                 'total_bytes': manifest['summary']['validated_output_bytes']}
    (run_dir / 'native-library-reference.json').write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding='utf-8')
    log['native_library'] = reference
    return [(library / directory, stage / directory, library) for directory in MOUNT_DIRECTORIES]

def digest(path):
    """计算输入或候选的文件指纹。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def prepare(args, run_dir, stage, log, phase_done):
    """准备完成后返回运行输入，不创建游戏进程。"""
    allowed = (ROOT / 'workspaces/offline-runs').resolve()
    pending_mounts = []
    shared_overlay = {}
    supplier = None
    native_library = getattr(args, 'native_library', None)
    if native_library and (args.overlay or args.resource_library or args.loose_library or args.supply_resources):
        raise ValueError('完整原生库不能与旧分库、overlay 或按需补件混用')
    image = args.image.resolve()
    if (image != (ROOT / 'bin/v1649370/codol1649370-offline.exe').resolve()
            and not image.is_relative_to((ROOT / 'workspaces').resolve())):
        raise ValueError('仅运行项目固定版本基底或 workspaces 内候选')
    # 运行时依赖（CEF/音频/图形 DLL 与语言包）已固化到项目固定资源目录，
    # 不再引用外部原客户端目录。
    deps_root = (ROOT / 'resources/v1649370/runtime-deps').resolve()
    if not native_library and not deps_root.is_dir():
        raise ValueError('缺少项目内运行时依赖目录：' + str(deps_root))
    exe = stage / 'codol1649370-offline.exe'
    shutil.copyfile(image, exe)
    log['input_image'] = {'path': str(image), 'sha256': digest(image)}
    for name in (() if native_library else DEPENDENCIES):
        source = deps_root / name
        if not source.is_file():
            raise ValueError('项目内运行时依赖缺失：' + name)
        shutil.copyfile(source, stage / name)
    locales = []
    for source in ([] if native_library else sorted((deps_root / 'locales').glob('*.pak'))):
        if not source.is_file() or not source.resolve().is_relative_to(deps_root):
            raise ValueError('CEF语言包来源越界')
        destination = stage / 'locales' / source.name
        destination.parent.mkdir(exist_ok=True)
        expected = digest(source)
        shutil.copyfile(source, destination)
        if digest(destination) != expected:
            raise ValueError('CEF语言包副本校验失败')
        locales.append({'path': 'locales/' + source.name, 'size': source.stat().st_size, 'sha256': expected})
    (run_dir / 'cef-locales-manifest.json').write_text(json.dumps(locales, ensure_ascii=False, indent=2), encoding='utf-8')
    log['cef_locales'] = {'count': len(locales), 'total_bytes': sum((item['size'] for item in locales))}
    (stage / 'iips.ini').write_text('[IIPS]\nenable_iips=0\n', encoding='ascii')
    for folder in ['players', 'localappdata', 'appdata', 'temp'] + ([] if native_library else ['main']):
        (stage / folder).mkdir()
    if native_library:
        pending_mounts.extend(prepare_native_library(native_library, stage, run_dir, log,
                                                     getattr(args, 'verify_resources', False)))
    shared_folders = set()
    if not args.supply_resources:
        if args.resource_library:
            shared_folders.add('main/images')
        if args.loose_library:
            shared_folders.update(('main/models', 'main/sound'))
    if args.overlay:
        overlay = args.overlay.resolve()
        if not any(overlay.is_relative_to(base.resolve()) for base in (ROOT / 'resources/v1649370', ROOT / 'workspaces')):
            raise ValueError('overlay 必须位于项目固定资源目录或 workspaces')
        for source in overlay.rglob('*'):
            if not source.is_file():
                continue
            relative = source.relative_to(overlay)
            if source.suffix.lower() in ('.exe', '.dll', '.sys') or source.name.lower() in FORBIDDEN:
                raise ValueError('资源overlay不可携带执行组件')
            if '/'.join(relative.parts[:2]).lower() in shared_folders:
                shared_overlay[relative.as_posix().lower()] = source
                continue
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    phase_done('private-runtime-files')
    for library_arg, library_kind in ((args.resource_library, 'iwi'), (args.loose_library, 'loose_startup_resources')):
        if library_arg is None:
            continue
        if library_kind == 'iwi':
            from codol_rebuild.resources.offline_resources import canonical_iwi as canonical_resource
        else:
            from codol_rebuild.resources.loose_library import canonical_loose_resource as canonical_resource
        library = library_arg.resolve()
        if not any(library.is_relative_to(base.resolve()) for base in (ROOT / 'resources/v1649370', ROOT / 'workspaces')):
            raise ValueError('资源库必须位于项目固定资源目录或 workspaces')
        manifest_path = library / 'manifest.json'
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        # 清单完整性规则统一由资源层提供；bootstrap 只负责装载与挂载。
        validate_library_manifest(manifest, library_kind)
        cache_audit = {}
        # 普通启动直接共享受保护的源库；诊断补件模式保留独立缓存。
        prepare_resources = prepare_cache if args.supply_resources else prepare_library
        receipt_root = ROOT / 'workspaces' / ('offline-resource-cache' if args.supply_resources else 'resource-library-verification')
        cache = prepare_resources(receipt_root, library, manifest_bytes, manifest['resources'], canonical_resource, protect_cache, audit=cache_audit, force_verify=getattr(args, 'verify_resources', False))
        names = set()
        total = 0
        mounted = set()
        for entry in manifest['resources']:
            name = entry['game_path']
            relative = Path(name)
            names.add(name)
            total += entry['size']
            folder = '/'.join(relative.parts[:2])
            if folder in shared_folders:
                if name in shared_overlay:
                    overlay_source = shared_overlay.pop(name)
                    if digest(overlay_source) != entry['sha256']:
                        raise ValueError('资源库与overlay内容冲突: ' + name)
                if folder not in mounted:
                    pending_mounts.append((cache / folder, stage / folder, cache))
                    mounted.add(folder)
                continue
            source = (cache / relative).resolve()
            destination = (stage / relative).resolve()
            if not source.is_relative_to(cache) or not destination.is_relative_to(stage):
                raise ValueError('资源库文件越界')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if destination.stat().st_size != entry['size'] or digest(destination) != entry['sha256']:
                    raise ValueError('资源库与overlay内容冲突: ' + name)
            elif destination.suffix.lower() == '.cfg':
                shutil.copyfile(source, destination)
            else:
                link_resource(source, destination)
        manifest_name = 'resource-library-manifest.json' if library_kind == 'iwi' else 'loose-library-manifest.json'
        (run_dir / manifest_name).write_text(json.dumps({'schema': 2, 'storage': 'shared-cache-reference' if args.supply_resources else 'source-library-reference', 'manifest_path': str(cache / 'manifest.json'), 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()}, indent=2), encoding='utf-8')
        log['resource_library' if library_kind == 'iwi' else 'loose_library'] = {'path': str(library), 'resource_root': str(cache), 'storage': 'directory-junctions' if mounted else 'hardlinks-with-private-config', 'validation': cache_audit, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'resource_count': len(names), 'total_bytes': total}
        phase_done('resources-' + library_kind)
    if shared_overlay:
        raise ValueError('共享目录存在未登记的overlay资源: ' + next(iter(shared_overlay)))
    from codol_rebuild.adapters.persistence.game_settings import SettingsStore, SettingsSession
    settings_store = SettingsStore(accounts_dir() / 'game-settings.sqlite3')
    settings_session = SettingsSession(settings_store, stage / 'players')
    log['game_settings'] = {'database': str(settings_store.path), 'restored_files': settings_session.restore()}
    pe = prepare_runtime_image(exe, args.patches, log)
    log['runtime_image_sha256'] = digest(exe)
    log['arguments'] = args.argument
    if args.supply_resources:
        from codol_rebuild.resources.offline_resources import OfflineResourceSupplier
        supplier = OfflineResourceSupplier(ROOT.parent, stage)
        if args.prewarm_run:
            previous = (allowed / args.prewarm_run).resolve()
            if previous.parent != allowed or previous == run_dir:
                raise ValueError('预热来源必须是既有的同项目会话')
            records = [json.loads(line) for line in (previous / 'resource-supply.jsonl').read_text(encoding='utf-8').splitlines()]
            names = sorted({row['logical_path'] for row in records if row.get('status') in ('supplied', 'already_present')})
            for name in names:
                supplied = supplier.supply(name)
                if supplied.get('status') not in ('supplied', 'already_present'):
                    raise ValueError('预热资源校验失败: ' + json.dumps(supplied, ensure_ascii=False))
            log['prewarm'] = {'requests_from': str(previous), 'verified_resource_count': len(names), 'method': 'revalidate current pinned sources before launch'}
    apply_runtime_acl(stage)
    from codol_rebuild.platform.resource_mount import mount_resource
    for source, destination, resource_root in pending_mounts:
        mount_resource(source, destination, resource_root, stage)
    phase_done('settings-patches-acl-mounts')
    return {'image': image, 'exe': exe, 'pe': pe, 'settings': settings_session, 'supplier': supplier}
