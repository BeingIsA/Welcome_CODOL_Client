"""把已保护的资源目录挂到单次运行目录；不复制或逐文件创建硬链接。"""
from pathlib import Path
import _winapi


def mount_resource(source, destination, resource_root, stage):
    source = Path(source).resolve()
    destination = Path(destination)
    stage = Path(stage).resolve()
    if not source.is_dir() or not source.is_relative_to(Path(resource_root).resolve()):
        raise ValueError('共享资源挂载来源越界')
    if destination.exists() or not destination.parent.resolve().is_relative_to(stage):
        raise ValueError('共享资源挂载目标冲突或越界')
    destination.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(source), str(destination))
    if destination.resolve() != source:
        raise ValueError('共享资源挂载回读失败')
