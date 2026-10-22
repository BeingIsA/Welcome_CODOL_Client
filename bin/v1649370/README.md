# 1649370 离线运行基底

`codol1649370-offline.exe` 是从原版客户端恢复代码、重建导入表并修复 PE 加载结构后生成的固定版本运行基底，供正式启动入口使用。

- 大小：11,075,584 字节。
- SHA-256：`55f067ac24e7d5ea9bea5acd46a1514eb1b7780a627b73c2094f41133dce3ffd`。
- 原始输入：`Original Client/codoMP_client_shipRetail.exe`。
- 恢复实现：`client/src/codol_rebuild/analysis/`；镜像构建：`client/src/codol_rebuild/adapters/game/v1649370/image.py`。

2026-10-21 从 `client/workspaces/offline-launch/` 迁入此目录，EXE 字节保持不变。随附 `offline-image.json` 是原始生成记录，其 source、output 等历史路径保留生成时的含义，不是当前运行配置。

启动入口和宿主默认从此目录读取基底，每次复制到 `client/workspaces/offline-runs/<会话>/runtime/` 后再应用运行补丁；不要直接修改这里的基底。临时构建候选仍可输出至 workspaces，经校验后再决定是否更新固定基底。该文件不能脱离宿主、守卫及资源供应链单独代表完整离线功能。
