# SPDX-License-Identifier: AGPL-3.0-or-later
"""独立运行目录的 Windows ACL 保护：授予当前用户完全控制并设置低完整性级别。"""
from __future__ import annotations
import subprocess
from pathlib import Path
from codol_rebuild.platform.win32 import current_sid


def apply_runtime_acl(stage: Path) -> None:
    """对独立 runtime 目录套用固定 ACL 规则；失败抛出 RuntimeError。"""
    # 先替换授权为当前用户完全控制，再设置低完整性级别，防止继承宽授权。
    for options in [('/grant:r', '*' + current_sid() + ':(OI)(CI)F'), ('/setintegritylevel', '(OI)(CI)L')]:
        acl = subprocess.run(['icacls', str(stage), *options], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if acl.returncode:
            raise RuntimeError('独立运行目录ACL失败: ' + (acl.stdout + acl.stderr).decode('mbcs', errors='replace'))
