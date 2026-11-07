"""按 Git 忽略规则打包本地版本资源；运行即创建标准 ZIP 分卷。"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import os


CLIENT_ROOT = Path(__file__).resolve().parents[1]
VOLUME_LIMIT = 2_500_000_000


def collect_resources(client_root: Path) -> list[Path]:
    """包含被忽略的版本资源；拒绝链接，避免跨目录读取或递归联接。"""
    resource_root = client_root / 'resources' / 'v1649370'
    candidates = []
    directories = [resource_root]
    while directories:
        directory = directories.pop()
        for resource_path in directory.iterdir():
            if resource_path.is_symlink() or resource_path.is_junction():
                raise ValueError('资源不能包含链接：' + str(resource_path))
            if resource_path.is_dir():
                directories.append(resource_path)
            elif resource_path.is_file():
                candidates.append(resource_path.relative_to(client_root).as_posix())
    # --no-index 同时识别尚未从索引移除、但已被规则忽略的旧清单。
    result = subprocess.run(
        ['git', '-C', str(client_root), 'check-ignore', '--no-index', '-z', '--stdin'],
        input=('\0'.join(candidates) + '\0').encode('utf-8'), capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
    return sorted(client_root / name.decode('utf-8') for name in result.stdout.split(b'\0') if name)


def write_archive(client_root: Path, resources: list[Path], output: Path, limit: int = VOLUME_LIMIT) -> None:
    """通过 Bandizip 创建标准 ZIP 分卷，在独立工作目录完成后发布整套文件。"""
    executable = shutil.which('bz.exe') or str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Bandizip/bz.exe')
    if not Path(executable).is_file():
        raise ValueError('请安装 Bandizip，或将 bz.exe 所在目录加入 PATH')
    if output.suffix.lower() != '.zip':
        raise ValueError('输出名称必须以 .zip 结尾')
    if output.resolve().is_relative_to((client_root / 'resources').resolve()):
        raise ValueError('输出不能位于资源目录中')
    staging = output.with_name(output.stem + '.partial')
    if staging.exists() or list(output.parent.glob(output.stem + '.z*')):
        raise ValueError('输出分卷或工作目录已存在，拒绝覆盖')
    staging.mkdir(parents=True)
    staged_output = staging / output.name
    command = ['c', '-fmt:zip', '-l:1', f'-v:{limit}', str(staged_output)]
    command.extend(resource.relative_to(client_root).as_posix() for resource in resources)
    command_path = staging / 'command.txt'
    command_path.write_text(subprocess.list2cmdline(command), encoding='utf-8-sig')
    before = [(resource.stat().st_size, resource.stat().st_mtime_ns) for resource in resources]
    # 命令文件绕过 Windows 命令行长度限制；不经 shell 解释路径。
    subprocess.run([executable, '/cmdfile', str(command_path)], cwd=client_root, check=True)
    after = [(resource.stat().st_size, resource.stat().st_mtime_ns) for resource in resources]
    if before != after:
        raise RuntimeError('打包期间资源发生变化，未发布分卷')
    volumes = sorted(staging.glob(output.stem + '.z*'))
    if not staged_output.exists() or any(volume.stat().st_size > limit for volume in volumes):
        raise ValueError('分卷不完整或超过大小限制，未发布')
    subprocess.run([executable, 't', str(staged_output)], check=True)
    for volume in volumes:
        volume.rename(output.parent / volume.name)
    command_path.unlink()
    staging.rmdir()


def main() -> None:
    """收集被忽略资源，检查空间并直接输出完整分卷包。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='输出名称基准，如 D:/packages/resources.zip；生成 .z01、.z02 和 .zip')
    options = parser.parse_args()
    resources = collect_resources(CLIENT_ROOT)
    required = CLIENT_ROOT / 'resources/v1649370/native-library-manifest.json'
    if required not in resources or not any(
            resource.is_relative_to(required.parent / 'game') for resource in resources):
        raise ValueError('缺少完整游戏资源或配套完成清单')
    total_bytes = sum(resource.stat().st_size for resource in resources)
    output = (options.output or CLIENT_ROOT / 'workspaces/resource-packages' /
              ('resources-1649370-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.zip')).resolve()
    print(f'文件数：{len(resources)}；原始大小：{total_bytes / 1024**3:.2f} GiB；输出：{output}')
    print('包含所有被 Git 忽略的版本资源、历史材料及生成清单；解压目标为 client 目录。')
    print(f'标准 ZIP 分卷，每卷不超过 {VOLUME_LIMIT:,} 字节；所有分卷放在一起，从 .zip 解压。')
    existing_parent = output.parent
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < total_bytes + total_bytes // 100 + 64 * 1024**2:
        raise ValueError('空间不足：按未压缩大小加余量预留，请使用 --output 指定其他磁盘')
    write_archive(CLIENT_ROOT, resources, output)
    print('全部分卷打包完成：' + str(output))


if __name__ == '__main__':
    main()
