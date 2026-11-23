"""按随项目交付的固定索引，从终版安装导入或补齐原包与配套文件。"""
import argparse
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile


CLIENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT_ROOT / 'src'))
from codol_rebuild.resources.packed_library import INDEX_NAME, read_packed_recipe


DEFAULT_PACKED_ROOT = CLIENT_ROOT / 'resources/v1649370/packed'


def file_sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def publish_file(source: Path, target: Path, record: dict) -> None:
    """同目录临时副本校验成功后原子发布，重跑时复核已经发布的文件。"""
    if target.resolve() != target:
        raise ValueError(f'发布路径发生重定向：{target}')
    if target.exists():
        if target.stat().st_size != record['size'] or file_sha256(target) != record['sha256']:
            raise ValueError(f'目标已存在且与终版不符：{target}')
        if source.stat().st_size != record['size'] or file_sha256(source) != record['sha256']:
            raise ValueError(f'源文件与终版不符：{source}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(source, temporary_path)
        if temporary_path.stat().st_size != record['size'] or file_sha256(temporary_path) != record['sha256']:
            raise ValueError(f'终版文件副本长度或摘要不符：{target.name}')
        temporary_path.replace(target)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare_packed_resources(original_root: Path, packed_root: Path = DEFAULT_PACKED_ROOT) -> None:
    """已有索引单独即可重建实体；全新输出目录沿用项目自带的同一固定索引。"""
    packed_root = packed_root.absolute()
    if packed_root.resolve() != packed_root:
        raise ValueError(f'发布目录发生重定向：{packed_root}')
    packed_root.mkdir(parents=True, exist_ok=True)
    target_index = packed_root / INDEX_NAME
    if target_index.exists() or packed_root == DEFAULT_PACKED_ROOT:
        index = read_packed_recipe(target_index)
    else:
        # 全新目标先校验独立索引副本，再发布；中断后可以沿用相同索引补齐实体。
        with tempfile.NamedTemporaryFile(dir=packed_root, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copyfile(DEFAULT_PACKED_ROOT / INDEX_NAME, temporary_path)
            index = read_packed_recipe(temporary_path)
            temporary_path.replace(target_index)
        finally:
            temporary_path.unlink(missing_ok=True)
    missing_bytes = sum(record['size'] for name, record in index.physical_files.items()
                        if not (packed_root / name).exists())
    if shutil.disk_usage(packed_root).free < missing_bytes:
        raise ValueError(f'磁盘空间不足，需复制 {missing_bytes:,} 字节')
    total_bytes = 0
    for position, (relative_name, record) in enumerate(index.physical_files.items(), 1):
        publish_file(original_root / relative_name, packed_root / relative_name, record)
        total_bytes += record['size']
        if position % 100 == 0:
            print(f'已核对 {position}/{len(index.physical_files)} 个文件，{total_bytes:,} 字节', flush=True)
    print(f'已发布 {len(index.physical_files)} 个实体文件，{total_bytes:,} 字节，'
          f'{len(index.members)} 个游戏路径。', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=CLIENT_ROOT.parent / 'Original Client',
                        help='仅用于生成发行包的终版原安装目录')
    parser.add_argument('--output', type=Path, default=DEFAULT_PACKED_ROOT,
                        help='自带原包目录，默认 client/resources/v1649370/packed')
    options = parser.parse_args()
    prepare_packed_resources(options.source.resolve(), options.output)
