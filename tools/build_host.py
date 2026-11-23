"""使用当前构建环境中的 Nuitka 编译可独立运行的 Windows 客户端宿主。"""
import os
from importlib.resources import files
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    client_root = Path(__file__).resolve().parents[1]
    output_directory = client_root / 'workspaces' / 'host-build'
    output_directory.mkdir(parents=True, exist_ok=True)
    build_environment = os.environ.copy()
    build_environment['PYTHONPATH'] = str(client_root / 'src')
    subprocess.run([
        sys.executable, '-m', 'nuitka', '--mode=standalone', '--msvc=latest',
        '--assume-yes-for-downloads', '--output-filename=CODOLHost.exe',
        f'--output-dir={output_directory}',
        f'--report={output_directory / "compilation-report.xml"}',
        str(client_root / 'tools' / 'launch_client.py'),
    ], cwd=client_root, env=build_environment, check=True)
    # Capstone 通过 ctypes 动态加载此 DLL，须保留 wheel 中的相对目录。
    library_directory = output_directory / 'launch_client.dist/capstone/lib'
    library_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files('capstone').joinpath('lib', 'capstone.dll'),
                 library_directory / 'capstone.dll')


if __name__ == '__main__':
    raise SystemExit(main())
