"""从公开的发行清单检查版本，并校验、安装程序包。"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.request import Request, urlopen


def release_request(url):
    return Request(url, headers={'User-Agent': 'CODOLLauncher/1.0'})
from zipfile import ZipFile


def version_number(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag):
        raise ValueError('发行版本号无效')
    return tuple(map(int, tag[1:].split('.')))


def check_release(base_url):
    if not base_url.startswith('https://'):
        raise ValueError('请配置 HTTPS 公开下载地址')
    with urlopen(release_request(base_url + '/releases/latest.json'), timeout=5) as response:
        release = json.loads(response.read(8193))
    version_number(release['version'])
    if release['path'] != f"releases/{release['version']}/CODOL-Windows-x64.zip":
        raise ValueError('程序包路径无效')
    if not re.fullmatch('[0-9a-f]{64}', release['sha256']) or not isinstance(release['size'], int) or release['size'] <= 0:
        raise ValueError('程序包校验信息无效')
    return release


def install_release(root, base_url, release, *, progress=None):
    # 先完整下载、核验并解包，再替换安装文件。
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        stage = Path(temporary)
        archive = stage / 'update.zip'
        with urlopen(release_request(base_url + '/' + release['path']), timeout=30) as response, archive.open('wb') as output:
            downloaded = 0
            while chunk := response.read(256 * 1024):
                downloaded += len(chunk)
                if downloaded > release['size']:
                    raise ValueError('下载文件校验失败')
                output.write(chunk)
                if progress:
                    progress(downloaded)
        with archive.open('rb') as stream:
            if archive.stat().st_size != release['size'] or hashlib.file_digest(stream, 'sha256').hexdigest() != release['sha256']:
                raise ValueError('下载文件校验失败')
        staged = stage / 'files'
        with ZipFile(archive) as package:
            if 'CODOL.exe' not in package.namelist():
                raise ValueError('更新包缺少 CODOL.exe')
            for entry in package.infolist():
                parts = entry.filename.rstrip('/').split('/')
                if any(part in ('', '.', '..') or part.endswith((' ', '.')) or os.path.isreserved(part) or '\\' in part or ':' in part for part in parts):
                    raise ValueError('更新包包含无效路径')
                if parts[0].lower() in {'resources', 'workspaces', 'data', 'codollauncher.exe', 'installed-version.json', 'update-url.txt'}:
                    continue
                package.extract(entry, staged)
        marker = 'installed-version.json'
        (staged / marker).write_text(json.dumps({'version': release['version']}), encoding='utf8')
        files = [path for path in staged.rglob('*') if path.is_file() and path != staged / marker]
        applied = []
        try:
            for source in [*files, staged / marker]:
                relative = source.relative_to(staged)
                target = root / relative
                if target.resolve() != target:
                    raise ValueError('安装路径包含重定向')
                if relative.parts[0].lower() == 'config' and target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                backup = None
                if target.exists():
                    backup = stage / 'backup' / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                os.replace(source, target)
                applied.append((target, backup))
        except Exception:
            for target, backup in reversed(applied):
                if backup is None:
                    target.unlink()
                else:
                    os.replace(backup, target)
            raise
