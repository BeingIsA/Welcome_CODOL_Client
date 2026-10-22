"""离线字符串净化短路：把背包改名依赖的在线净化请求改成本地立即完成。

改名请求经 AS 的 SanitizeController 广播 EVT_SANITIZE_STRING，再向在线服务发起
TENCENT_SANITIZE_STRING 请求；离线没有该服务时请求不会回调，弹窗永久等待
EVT_ON_STRING_SANITIZED。本模块只做两处等长方法替换：

1. SanitizeController.sanitize 末尾把在线 request 换成广播净化完成事件；
2. WeaponLoadOutController.onSetLoadoutNameHandle 在参数对象里补 sanitized=false。

sanitized 表示内容被过滤，而不是检查成功。离线保留原名称，不提供在线敏感词过滤。

净化控制器所在 ABC 的字符串池没有 sanitized，该字段只能由配装发起端补齐。
替换保持方法体长度、栈上限与局部变量数量不变，不修改 UI 布局、在线服务状态或资源文件。
"""
from __future__ import annotations

from codol_rebuild.client.v1649370 import ui_movie

SANITIZE_OFFSET = 0x484C39
SANITIZE_LENGTH = 164
# 尾部 debugline 之后的在线请求序列，长度 20 字节。
SANITIZE_TAIL_BEFORE = bytes.fromhex('f04fd05d2566256626d1d0662746280329f05047')
SANITIZE_TAIL_AFTER = bytes.fromhex(
    'd0'            # getlocal0
    '5d026602660f'  # BCCommonEvent.EVT_ON_STRING_SANITIZED
    'd1'            # getlocal1
    '461102'        # broadcast(this, event, sanitizeObj)
    '29'            # pop
    '0202020202'    # nop 填充，保持原长度
    'f050'          # 原 debugline 80
    '47')           # returnvoid

LOADOUT_OFFSET = 0x4FB77E
LOADOUT_LENGTH = 59
LOADOUT_BEFORE = bytes.fromhex(
    'f10cf09001d030ef0166009001f09201d0d166556656615bf09301d05d366636665c2c735d5d665d665e'
    '2c7cd16655665f550246430229f0940147')
LOADOUT_AFTER = bytes.fromhex(
    'f09001d030d0d166556656615bf09301d05d366636665c2c735d5d665d665e2c7cd16655665f5502'
    '2a27'          # dup; pushfalse：内容未被过滤，原版接收端才会写入名称
    '61bb01'        # setproperty sanitized
    '464302'        # callproperty broadcast 2
    '29f0940147'    # pop; debugline 148; returnvoid
    '020202020202')  # 删除调试指令后补齐原长度


def replacements() -> list:
    """返回本模块登记的替换规则，由主电影编排层统一校验与应用。"""
    sanitize_tail = SANITIZE_OFFSET + SANITIZE_LENGTH - len(SANITIZE_TAIL_BEFORE)
    if len(SANITIZE_TAIL_BEFORE) != len(SANITIZE_TAIL_AFTER):
        raise ValueError('净化方法替换长度不一致')
    if len(LOADOUT_BEFORE) != LOADOUT_LENGTH or len(LOADOUT_AFTER) != LOADOUT_LENGTH:
        raise ValueError('配装方法替换长度不一致')
    return [(sanitize_tail, SANITIZE_TAIL_BEFORE, SANITIZE_TAIL_AFTER),
            (LOADOUT_OFFSET, LOADOUT_BEFORE, LOADOUT_AFTER)]


def patch_movie(original: bytes) -> bytes:
    """仅应用净化短路修正；整体指纹与原始字节由编排层校验。"""
    return ui_movie.apply_rules(original, replacements())
