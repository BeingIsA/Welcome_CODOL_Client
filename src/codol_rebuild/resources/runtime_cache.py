"""校验共享资源库并保存元数据记录；资源文件直接复用，不建立副本缓存。"""
import hashlib
import json
import os
from pathlib import Path
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


def prepare_library(root, library, manifest_bytes, entries, canonical, protect, *, audit=None, force_verify=False,
                    external_manifest=None):
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
    # 完整原生库把清单放在树外，树内所有文件均须登记；旧库仅挂接 main。
    runtime_names = set(before) if external_manifest is not None else {name for name in before if name.startswith('main/')}
    if runtime_names != names:
        raise ValueError('资源库包含缺失或未登记的运行文件')
    source_manifest = Path(external_manifest) if external_manifest is not None else library / 'manifest.json'
    if source_manifest.read_bytes() != manifest_bytes:
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
