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
