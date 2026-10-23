# 2026-10-22 源码快照与轨迹基线

冻结于问题 2（改名卡数量扣除）、7（资源子系统重构）、8（业务规则解耦）、
10（UI 补丁循环依赖）修复之后的 `adapters/game/v1649370` 当前源码快照，
用于守护原生生成器与安装轨迹不被无意修改：

- `client/v1649370/*.py`：当前模块逐字节快照，`test_native_generators_unchanged`
  逐模块比较 AST；有意修改时应重新冻结本目录并说明原因。
- `install-trace-online.json` / `install-trace-local.json`：当前桥
  `install_menu_preview` 在固定镜像内存模型下的字节级黄金轨迹（写入、
  页保护、断点、陷阱与适配器状态），取代 10-20 冻结单体的轨迹差分——
  问题 2 引入的消费回写钩子使旧单体轨迹不再等价。

文件 SHA-256 见 manifest.json。一次性迁移差分仍使用
`refactor-20261020`（账号初始化等价用例）。运行捕获样本见
`local-profile-capture`。

## 重冻结记录

- 2026-10-23：经批准接入加入房间，更新 `room.py` 与 `multiplayer_bridge.py`
  快照。新增 AS_JoinRoom 字符串捕获、独立入房通知、成员/设置快照转换和
  取消清理；未注入加入入口时的旧房间生成字节仍由 10-20 基线独立校验。
  新行为由 HTTP/应用层/桥接测试及 x86 加入回调执行夹具覆盖，不代表实机
  多人 UI 或持续成员同步已经验收。

- 2026-10-23：经批准接入服务端大厅列表，更新 `multiplayer_bridge.py` 快照。
  联机安装新增列表适配器，帧生成器通过可选发布入口驱动 GFx 列表发布，
  并保持查询/取消结果消费期间的轮询。`list_publish=None` 的既有生成路径
  保持不变；本目录安装轨迹用例不注入联机端口，原轨迹不变。列表 ABI
  由 `test_room_list.py` 与独立 x86 执行测试 `test_room_list_native.py` 覆盖。

- 2026-10-23：`multiplayer_bridge.py` 修复房内改设置/改地图不生效——
  `_submit_settings` 提交后未武装 `POLL`，建房收尾把 `POLL` 归 0 后设置结果
  再无人消费，通道被首个 future 永久占住，后续改动全被丢成 busy。改动只在
  运行时处理函数（`_submit_settings`、`_poll` 的设置分支），未触及任何原生
  代码生成器，故 install-trace 黄金轨迹无需重冻结。
