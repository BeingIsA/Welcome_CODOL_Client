"""工具运行环境检查：只读列举进程，禁止修改运行中的游戏存档。"""
import csv
import io
import os
import subprocess
from pathlib import Path


def ensure_game_stopped():
    """检查已知终版客户端进程；检查失败时拒绝写入。"""
    tasklist = Path(os.environ['SystemRoot']) / 'System32/tasklist.exe'
    result = subprocess.run([str(tasklist), '/FO', 'CSV', '/NH'], capture_output=True,
        text=True, errors='replace', creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=True)
    for row in csv.reader(io.StringIO(result.stdout)):
        if not row:
            continue
        process_name = row[0].casefold()
        if process_name in {'codol1649370-offline.exe', 'codomp_client_shipretail.exe', 'codol.exe'} or process_name.startswith('codomp_'):
            raise ValueError('检测到游戏仍在运行，请退出游戏后再解锁。')
