"""编译 Windows 图形宿主，装配独立程序包；游戏资源单独部署。"""
import argparse
import os
from importlib.resources import files
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

CLIENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT_ROOT / 'src'))
from codol_rebuild.resources.packed_view import PACKED_READER_FILES


def compile_host(build_directory: Path) -> Path:
    build_environment = os.environ.copy()
    build_environment['PYTHONPATH'] = str(CLIENT_ROOT / 'src')
    subprocess.run([
        sys.executable, '-m', 'nuitka', '--mode=standalone', '--msvc=latest',
        '--python-flag=isolated',
        '--assume-yes-for-downloads', '--output-filename=CODOL.exe',
        '--windows-console-mode=attach',
        f'--windows-icon-from-exe={CLIENT_ROOT / "bin/v1649370/codol1649370-offline.exe"}',
        f'--output-dir={build_directory}',
        f'--report={build_directory / "compilation-report.xml"}',
        str(CLIENT_ROOT / 'tools' / 'launch_client.py'),
    ], cwd=CLIENT_ROOT, env=build_environment, check=True)
    compiled_directory = build_directory / 'launch_client.dist'
    # Capstone 通过 ctypes 动态加载此 DLL，须保留 wheel 中的相对目录。
    library_directory = compiled_directory / 'capstone/lib'
    library_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files('capstone').joinpath('lib', 'capstone.dll'),
                 library_directory / 'capstone.dll')
    return compiled_directory


def assemble_release(compiled_directory: Path, destination: Path) -> None:
    shutil.copytree(compiled_directory, destination)
    runtime_files = (
        'bin/offline_guard.exe', 'bin/v1649370/codol1649370-offline.exe', 'config/server.ini',
        'manifests/offline-menu-patches.json',
        *(f'bin/v1649370/packed-reader/{name}' for name in PACKED_READER_FILES),
    )
    for relative_path in runtime_files:
        source = CLIENT_ROOT / relative_path
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for path in destination.rglob('*'):
        if path.suffix.lower() in {'.py', '.pyw', '.pyi', '.pyc', '.pyo'}:
            raise ValueError(f'发行目录含 Python 源文件或字节码：{path}')
    # --help 会加载编译入口，又允许完全没有游戏资源的开发机发布程序。
    subprocess.run([destination / 'CODOL.exe', '--help'], stdout=subprocess.DEVNULL,
                   cwd=destination.parent, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='独立程序发行目录，指定尚不存在的新目录')
    options = parser.parse_args()
    build_root = CLIENT_ROOT / 'workspaces/host-build'
    build_root.mkdir(parents=True, exist_ok=True)
    if options.output is None:
        print(compile_host(build_root))
        return
    output = options.output.resolve()
    if output.exists() or output.is_relative_to(CLIENT_ROOT / 'resources'):
        parser.error('请指定资源目录以外、尚不存在的发行目录')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=build_root, prefix='compile-') as build_directory:
        compiled_directory = compile_host(Path(build_directory))
        # 普通目录继承交付位置的 ACL，提权守卫与普通宿主才能交换回执。
        staging = output.parent / ('.codol-stage-' + uuid4().hex)
        staging.mkdir()
        try:
            staged_release = staging / 'release'
            assemble_release(compiled_directory, staged_release)
            staged_release.rename(output)
        finally:
            shutil.rmtree(staging)
    print(f'发行目录：{output}')


if __name__ == '__main__':
    raise SystemExit(main())
