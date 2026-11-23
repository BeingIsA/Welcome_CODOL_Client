"""使用本机 Visual Studio 和 Windows SDK 构建正式的 IFS 读取器与 Cloud Files 适配库。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


CLIENT_ROOT = Path(__file__).resolve().parents[1]


def compiler_environment(architecture):
    """载入对应体系结构的 MSVC 环境；构建物不依赖开发者命令行。"""
    locator = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    installation = subprocess.check_output([
        str(locator), '-latest', '-products', '*', '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '-property', 'installationPath', '-utf8',
    ], encoding='utf-8').strip()
    if not installation:
        raise RuntimeError('需要安装 Visual Studio C++ x86/x64 工具集')
    setup = Path(installation) / 'VC/Auxiliary/Build/vcvarsall.bat'
    settings = subprocess.check_output(
        f'cmd.exe /d /u /c call "{setup}" {architecture} >nul && set',
        executable=os.environ['COMSPEC'], encoding='utf-16-le',
    )
    return dict(line.split('=', 1) for line in settings.splitlines() if '=' in line and not line.startswith('='))


def main():
    destination = CLIENT_ROOT / 'bin/v1649370/packed-reader'
    destination.mkdir(parents=True, exist_ok=True)
    workspace = CLIENT_ROOT / 'workspaces'
    workspace.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workspace, prefix='packed-reader-build-') as build:
        for architecture, name in [('x86', 'ifs_reader.exe'), ('x64', 'cloud_files.dll')]:
            environment = compiler_environment(architecture)
            compiler = shutil.which('cl.exe', path=environment.get('Path', environment.get('PATH')))
            source = CLIENT_ROOT / 'native' / (Path(name).stem + '.cpp')
            options = ['/nologo', '/utf-8', '/std:c++17', '/EHsc', '/MT', '/O2', '/W4', '/Fe:' + name]
            if name.endswith('.dll'):
                options += ['/LD', str(source), '/link', 'CldApi.lib']
            else:
                options.append(str(source))
            subprocess.run([compiler, *options], cwd=build, env=environment, check=True)
        for name in ('ifs_reader.exe', 'cloud_files.dll'):
            os.replace(Path(build) / name, destination / name)


if __name__ == '__main__':
    main()
