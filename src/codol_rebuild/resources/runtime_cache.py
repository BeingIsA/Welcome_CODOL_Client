"""建立独立、按清单指纹寻址的运行资源缓存，供会话硬链接复用。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree_stamp(root):
    """枚举文件身份、大小及时间戳；拒绝重解析点，不读取文件内容。"""
    root = Path(root)
    result = {}
    def visit(directory, prefix):
        with os.scandir(directory) as children:
            for child in children:
                st = child.stat(follow_symlinks=False)
                if child.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('资源目录不可包含重解析点: ' + child.path)
                name = prefix + child.name
                if child.is_dir(follow_symlinks=False):
                    visit(child.path, name + '/')
                else:
                    result[name] = [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    visit(root, '')
    return result


def save_receipt(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    pending.write_text(json.dumps(value, separators=(',', ':')), encoding='utf-8')
    os.replace(pending, path)


def prepare_library(root, library, manifest_bytes, entries, canonical, protect, *, audit=None, force_verify=False):
    """校验并保护源资源库，普通会话直接挂接；工作目录仅保存校验记录。"""
    library = Path(library).resolve()
    key = hashlib.sha256(manifest_bytes + str(library).encode('utf-8')).hexdigest()
    receipt = Path(root) / (key + '.json')
    names = set()
    for entry in entries:
        name = entry['game_path']
        if canonical(name) != name or name in names:
            raise ValueError('资源库路径非法或重复: ' + name)
        names.add(name)
    before = tree_stamp(library)
    # 构建报告和被替代资源留在库内，但挂接的 main 中不能混入未登记内容。
    if {name for name in before if name.startswith('main/')} != names:
        raise ValueError('资源库包含缺失或未登记的运行文件')
    if (library / 'manifest.json').read_bytes() != manifest_bytes:
        raise ValueError('资源库清单发生变化')
    current = {'schema': 1, 'library': str(library), 'source': before}
    try:
        previous = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = None
    if not force_verify and previous == current:
        if audit is not None:
            audit.update(validation='unchanged-metadata', hashed_bytes=0, copied_bytes=0)
        return library
    for entry in entries:
        source = library / entry['game_path']
        if source.stat().st_size != entry['size'] or digest(source) != entry['sha256']:
            raise ValueError('资源库文件指纹不符: ' + entry['game_path'])
    if tree_stamp(library) != before:
        raise ValueError('校验过程中资源发生变化')
    protect(library)
    after = tree_stamp(library)
    # ACL 设置可能改变 ctime；内容身份、大小和修改时间必须保持不变。
    if {k: v[:3] for k, v in after.items()} != {k: v[:3] for k, v in before.items()}:
        raise ValueError('保护过程中资源发生变化')
    save_receipt(receipt, {'schema': 1, 'library': str(library), 'source': after})
    if audit is not None:
        audit.update(validation='full-sha256', hashed_bytes=sum(e['size'] for e in entries), copied_bytes=0)
    return library


def prepare_cache(root, library, manifest_bytes, entries, canonical, protect, *, audit=None, force_verify=False):
    """记录未变时仅扫描元数据；变更或强制复核时完整校验，首次独立复制。"""
    root = Path(root).resolve()
    library = Path(library).resolve()
    key = hashlib.sha256(manifest_bytes).hexdigest()
    cache = root / key
    receipt = root / '.verified' / (key + '.json')
    source_stamp = tree_stamp(library)
    cache_stamp = tree_stamp(cache) if cache.is_dir() and cache.resolve() == cache else None
    current = {'schema': 1, 'library': str(library), 'source': source_stamp, 'cache': cache_stamp}
    try:
        previous = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = None
    if not force_verify and cache_stamp is not None and previous == current:
        if audit is not None: audit.update(validation='unchanged-metadata', hashed_bytes=0)
        return cache
    if audit is not None: audit.update(validation='full-sha256', hashed_bytes=0)
    if cache_stamp is not None and set(cache_stamp) != {e['game_path'] for e in entries} | {'manifest.json'}:
        raise ValueError('共享缓存包含缺失或未登记文件')
    names = set()
    for entry in entries:
        name = entry['game_path']
        if canonical(name) != name or name in names:
            raise ValueError('资源库路径非法或重复: ' + name)
        names.add(name)
        source = (library / name).resolve()
        if not source.is_relative_to(library):
            raise ValueError('资源库文件越界')
        if source.stat().st_size != entry['size'] or digest(source) != entry['sha256']:
            raise ValueError('资源库文件指纹不符: ' + name)
    if cache.exists():
        if cache.resolve() != cache:
            raise ValueError('共享缓存不可重定向')
        if (cache / 'manifest.json').read_bytes() != manifest_bytes:
            raise ValueError('共享缓存清单指纹不符')
        for entry in entries:
            path = cache / entry['game_path']
            if not path.resolve().is_relative_to(cache):
                raise ValueError('缓存文件越界')
            if path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
                raise ValueError('共享缓存指纹不符: ' + entry['game_path'])
        after = {'schema': 1, 'library': str(library), 'source': tree_stamp(library), 'cache': tree_stamp(cache)}
        if after['source'] != source_stamp or after['cache'] != cache_stamp:
            raise ValueError('校验过程中资源发生变化')
        save_receipt(receipt, after)
        if audit is not None: audit['hashed_bytes'] = sum(e['size'] for e in entries) * 2
        return cache
    root.mkdir(parents=True, exist_ok=True)
    pending = root / ('.building-' + uuid.uuid4().hex)
    pending.mkdir()
    # 未完成的目录不会作为缓存使用；保留失败现场便于诊断。
    for entry in entries:
        target = pending / entry['game_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(library / entry['game_path'], target)
        if digest(target) != entry['sha256']:
            raise ValueError('缓存复制复核失败: ' + entry['game_path'])
    (pending / 'manifest.json').write_bytes(manifest_bytes)
    protect(pending)
    try:
        pending.rename(cache)
    except FileExistsError:
        # 并发发布时仍复核获胜者，不覆盖其内容。
        return prepare_cache(root, library, manifest_bytes, entries, canonical, protect)
    if tree_stamp(library) != source_stamp:
        raise ValueError('构建过程中资源库发生变化')
    save_receipt(receipt, {'schema': 1, 'library': str(library), 'source': source_stamp, 'cache': tree_stamp(cache)})
    if audit is not None: audit['hashed_bytes'] = sum(e['size'] for e in entries) * 2
    return cache


def link_resource(source, destination):
    """仅链接已保护的缓存；已有 overlay 必须相同，绝不修改共享文件。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if digest(source) != digest(destination):
            raise ValueError('资源库与 overlay 内容冲突: ' + str(destination))
        return
    os.link(source, destination)
