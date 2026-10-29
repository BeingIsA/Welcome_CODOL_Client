"""固定版本主电影资源的组合补丁编排：汇总各适配模块的替换规则并统一应用。

同一份 codo.gfx 承载多处不同职责的 ABC 方法修正（大厅空赛季保护、离线字符串净化短路等），
它们位于不同 DoABC 段且互不重叠。本模块只负责规则收集顺序；资源身份常量与
校验、打包、扫描能力由 movie_identity 提供（本模块转发以兼容既有调用），
具体方法字节仍由各适配模块提供，不修改磁盘资源、共享资源包或在线接口。
"""
from __future__ import annotations

from codol_rebuild.adapters.game.v1649370 import movie_identity
from codol_rebuild.adapters.game.v1649370.movie_identity import (  # noqa: F401 兼容既有调用
    LOAD_BYTES as LOAD_BYTES,
    LOAD_RETURN_SITE as LOAD_RETURN_SITE,
    LOAD_RETURN_BYTES as LOAD_RETURN_BYTES,
    LOAD_SITE as LOAD_SITE,
    COMPRESSED_SIZE as COMPRESSED_SIZE,
    RESOURCE_NAME as RESOURCE_NAME,
    RESOURCE_SHA256 as RESOURCE_SHA256,
    RESOURCE_SIZE as RESOURCE_SIZE,
    apply_rules,
    build_resource_patch as _build_resource_patch,
    find_resource_patch as _find_resource_patch,
)


def replacements() -> list:
    """汇总各适配模块的替换规则；规则提供者不反向依赖本模块，无循环依赖。"""
    from codol_rebuild.adapters.game.v1649370.accounts import player_name, ui_recharge, purchase
    from codol_rebuild.adapters.game.v1649370.frontend import sanitize, ui_navigation, ui_theme, ui_weekly_rewards
    from codol_rebuild.adapters.game.v1649370.rooms import room, room_member_menu
    from codol_rebuild.adapters.game.v1649370.chat import ui_chat

    return (list(ui_navigation.replacements()) + list(sanitize.replacements())
            + list(player_name.replacements()) + list(ui_theme.replacements())
            + list(ui_weekly_rewards.replacements()) + list(ui_recharge.replacements())
            + list(purchase.replacements()) + list(room_member_menu.replacements())
            + list(ui_chat.replacements()) + list(room.replacements()))


def patch_movie(original: bytes) -> bytes:
    """应用全部已登记替换，供离线会话内注入使用。"""
    return apply_rules(original, replacements())


def build_resource_patch(read, header_address: int, rules=None) -> dict:
    """校验重定位 RawFile 头及完整解压内容，返回数据和长度两项显式写计划。"""
    return _build_resource_patch(read, header_address,
                                  replacements() if rules is None else rules)


def find_resource_patch(read, regions, rules=None) -> dict:
    """扫描精确长度对后校验名称与完整资源指纹；歧义或缺失时拒绝修改。"""
    return _find_resource_patch(read, regions,
                                replacements() if rules is None else rules)
