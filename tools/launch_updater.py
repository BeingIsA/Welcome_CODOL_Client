"""Windows 更新启动器，使用当前用户权限检查更新并启动游戏。"""
import ctypes
import json
import msvcrt
from pathlib import Path
import subprocess
import sys

from codol_rebuild.updater import check_release, install_release, version_number


def dialog(message, flags=0):
    return ctypes.windll.user32.MessageBoxW(None, message, 'CODOL 启动器', flags)


def main():
    root = Path(sys.argv[0]).resolve().parent
    with (root / '.launcher.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            dialog('另一个启动器正在运行。')
            return
        try:
            base_url = 'https://pub-aa6c360bfe0e4f8eba38db385971c090.r2.dev'
            release = check_release(base_url)
            marker = root / 'installed-version.json'
            current = json.loads(marker.read_text(encoding='utf8'))['version'] if marker.exists() else None
            build_commit = root / 'release-commit.txt'
            if not current and build_commit.exists() and build_commit.read_text().strip() == release.get('commit'):
                current = release['version']
                marker.write_text(json.dumps({'version': current}), encoding='utf8')
            if current is None or version_number(current) < version_number(release['version']):
                if dialog(f"本机版本：{current or '未知'}\n可用版本：{release['version']}\n立即下载并安装？选择“否”可跳过更新。", 4) == 6:
                    install_release(root, base_url, release)
        except Exception as error:
            if dialog(f'更新未完成：{error}\n是否启动已安装的客户端？', 4) != 6:
                return
        subprocess.Popen([str(root / 'CODOL.exe')], cwd=root)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        dialog(str(error), 16)
