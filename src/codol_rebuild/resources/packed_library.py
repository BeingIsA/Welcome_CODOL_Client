"""终版自带资源包的独立索引。启动仅依赖 packed 目录内的物理文件。"""
from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import stat


INDEX_NAME = 'packed-index.json.gz'
# 固定未压缩索引的摘要，避免压缩工具版本成为资源版本的一部分。
INDEX_SHA256 = '1fc7aff6e8988be2cb7a9a574575489b8933e9072b66dc7cc685f2f10b77b1d3'


@dataclass(frozen=True)
class PackedIndex:
    """成员记录按游戏路径检索，实体记录按发布目录内的相对路径检索。"""

    sha256: str
    members: dict[str, dict]
    physical_files: dict[str, dict]


def read_packed_recipe(index_path: Path) -> PackedIndex:
    """只读固定摘要的发行索引，允许在资源实体尚未导入时取得导入清单。"""
    index_path = Path(index_path).absolute()
    if index_path.resolve() != index_path:
        raise ValueError(f'终版索引不能重定向到其他位置：{index_path}')
    with gzip.open(index_path, 'rb') as stream:
        index_bytes = stream.read()
    digest = hashlib.sha256(index_bytes).hexdigest()
    if digest != INDEX_SHA256:
        raise ValueError(f'终版资源索引摘要不符：{index_path}')
    index = json.loads(index_bytes)
    members = index['members']
    for game_path, member in members.items():
        member['game_path'] = game_path
    return PackedIndex(digest, members, index['physical_files'])


def read_packed_index(packed_root: Path, *, verify_resources: bool = False) -> PackedIndex:
    """检查终版索引和包内实体；深度校验直接读取 IFS，不触发占位文件填充。"""
    packed_root = Path(packed_root).absolute()
    index = read_packed_recipe(packed_root / INDEX_NAME)
    for relative_name, record in index.physical_files.items():
        path = packed_root / relative_name
        if path.resolve() != path:
            raise ValueError(f'终版资源文件发生重定向：{relative_name}')
        status = path.stat()
        if not stat.S_ISREG(status.st_mode):
            actual_type = '目录' if stat.S_ISDIR(status.st_mode) else stat.filemode(status.st_mode)
            raise ValueError(f'终版资源文件类型不符：{path}，期望为普通文件，实际为{actual_type}')
        if status.st_size != record['size']:
            raise ValueError(
                f"终版资源文件长度不符：{path}，索引要求 {record['size']} 字节，实际 {status.st_size} 字节"
            )
        if verify_resources:
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                    raise ValueError(f'终版资源文件摘要不符：{relative_name}')
    return index
