"""1649370 本地 UI 服务替代：生成缺失信用服务和欢迎页状态的固定补丁。

保留原生回调 ABI；不伪造评分，不修改在线服务状态，安装由会话桥负责。
"""
from __future__ import annotations

def local_optional_service_patches():
    """Local provider has no WarGod credit service; its void request is a no-op.

    AS_FetchWarGodCreditRatingScore calls 63F9A0, which passes a null
    DemonWare service to B8A420 offline. Keep the original RET 4 callback ABI,
    return no invented score, and do not modify any shared network routine.
    """
    return [(0x6AD010, bytes.fromhex('e88b29f9ff'), b'\x90' * 5)]


def local_welcome_page_patch():
    """Suppress automatic welcome/MOTD opening for the local provider.

    IsWelcomePageShowed (5CC4F0, vtable DC8238) normally loads BL from
    84AD728. Report true without changing that global or the manual MOTD
    entry. Keep the GFx Boolean assignment, managed-value cleanup and RET 4.
    Only the host's --local-profile branch installs this runtime patch.
    """
    return (0x5CC4F7, bytes.fromhex('8a1d28d74a08'), bytes.fromhex('b30190909090'))
