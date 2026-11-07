# 固定版本运行资源

## Git 更新与资源分卷

Git 只维护本说明、`offline-menu-patches.json` 及四份固定构建依据：
`iips-patch-priority-final.json`、`startup-non-iwi-resources.json`、
`startup-resource-content-conflicts-v2.json`、`common-mp-fragment-conflicts.json`。
其余版本资源由本地资源包分发，完成清单必须与 `game/` 一起更新。

在项目根目录执行 `python -B -X utf8 client/tools/package_resources.py` 直接打包，
无需额外开关；可用 `--output D:/packages/resources.zip` 指定输出名称。
默认输出到 `client/workspaces/resource-packages/`，生成标准 `.z01`、`.z02`、`.zip` 分卷，
每卷最多 2,500,000,000 字节；全部下载到同一目录，从最后的 `.zip` 解压到 `client/`。
脚本按 Git 忽略规则收集全部版本资源（包括历史材料和生成报告），不包含上述 Git 文件。
打包需要安装 Bandizip（默认安装路径或 PATH 中的 `bz.exe`），Python 负责资源筛选。
打包期间不要构建或修改资源；失败材料保留在 `.partial` 目录，已有分卷不会被覆盖。
发布前执行压缩包完整性测试，单个大文件可跨卷存放。
日常代码与补丁更新使用 `git pull`，原生资源变更则需要另行更新配套资源包。

本目录存放 1649370 客户端的固定运行数据，由资源准备工具发布、资源层校验，再由 bootstrap 装配到运行会话。源码模块职责见[客户端架构设计](../../docs/架构设计.md)。

## 目录内容

| 路径 | 内容与使用方式 |
|---|---|
| `game/` | 标准启动的完整原生资源根：`main/`、`hires/`、`client_shipretail/`、`locales/` 及根级运行依赖；仅完成清单存在且校验通过时可用 |
| `native-library-plan.json` | 974 包真实提取收据、原生覆盖顺序、独立视频及运行依赖、预期尺寸和空间核算 |
| `native-library-build.json` | 最近一次全量构建结果，包含来源、摘要、被替换候选和错误；失败结果不可供启动使用 |
| `native-library-manifest.json` | 全量构建成功才发布的完成清单，位于 `game/` 外；供启动前完整性校验使用 |
| `offline-resources/` | 历史基础覆盖层，保留用于迁移比对，旧启动路径已移除，不参与完整资源标准启动 |
| `offline-image-library/` | 历史图片库及清单，保留用于迁移比对，旧启动路径已移除 |
| `offline-loose-library/` | 历史模型、音频及指定配置库，保留用于迁移比对，旧启动路径已移除 |
| `offline-resources-manifest*.json` | 基础资源来源、处理方式与摘要记录 |
| `offline-menu-patches.json` | 七项运行补丁（含横纵灵敏度下限移除），仅保存地址、原字节和目标字节；启动入口校验后由版本适配应用到会话镜像 |
| `iips-patch-priority-final.json` | 资源覆盖规则的固定依据，供应器核对摘要与候选指纹后选择资源 |
| `startup-non-iwi-resources.json` | 模型、音频及指定配置资源的固定审计清单，构建时核对摘要和完整性 |
| `startup-resource-content-conflicts-v2.json`、`common-mp-fragment-conflicts.json` | 图片库构建使用的冲突记录，后者为前者缺失时的回退输入 |
| `runtime-deps/` | 从原版客户端固化的原生 DLL、CEF 数据与语言包；用于完整库构建，正式启动复用已准备的程序槽位 |

## 资源处理与运行装配

原始资源包的输入副本保存在 `client/workspaces/offline-resource-inputs/`，按来源包提取的中间材料及收据保存在 `client/workspaces/offline-resource-raw/`。`python -B -X utf8 client/tools/prepare_native_resources.py` 从项目根目录运行时只生成清单与空间核算，添加 `--build` 才发布完整资源。工具使用已有提取材料，不重新提取 IFS；构建时核对 `Original Client` 的固定版本镜像和 974 个源包，并纳入其 `main/video` 与已固定的 `runtime-deps`。

普通同名完整文件按已有原生 FileItem 挂载顺序由后者覆盖前者；被替换候选保留尺寸与摘要证据，最终候选执行散装 AES/zlib 解码和类型检查，或 Fastfile 签名、哈希链、解压与 Zone 目录校验。FF 仍按原始文件格式发布，未展开成 `.zone`。不能把提取材料存在、外层校验通过或目录齐全视为所有资源内部结构和游戏玩法已经兼容。

标准入口 `start_client.cmd` / `tools/launch_client.py` 使用 `game/`。程序目录位于 `client/workspaces/runtime-programs/<版本指纹>/slot-编号/runtime`，EXE、根级 DLL/CEF 数据和磁盘补丁只在新槽位准备一次；之后校验并复用，不再每次复制。多开独占不同槽位，退出释放。资源仅在 `main`、`hires`、`client_shipretail`、`locales` 四个顶层建立联接，树内没有嵌套联接或资源硬链接。`players`、`appdata`、`localappdata`、`temp` 另以顶层联接指向当前独立会话；设置长期保存在系统用户目录。共享程序和资源保持中完整性保护，低完整性游戏可读而不可写；每次启动仍核对完整资源文件集合，元数据变化或 `--verify-resources` 时复核所有资源摘要。

完整资源模式不能与旧分库、overlay、按需补件混用。旧分库参数仅供显式诊断，旧数据在迁移期间保留，不作为完整模式的回退来源。标准启动不依赖 `Original Client`、提取目录、验证目录或旧分库；这些只用于重建和调查。

`client/workspaces/offline-resource-cache/` 是从已验证资源库生成的可重建共享缓存，由内容清单定位并验证。每次运行在 `client/workspaces/offline-runs/` 中创建独立程序副本、配置及资源链接；清理会话不得遍历资源目录联接，清理共享缓存须考虑现有会话引用。输入副本和 raw 中间材料仍供构建与诊断补件使用，不能仅因位于工作目录就整体删除。

账号与游戏设置保存在 `client/data/accounts/`，不属于资源库，也不随资源重建覆盖。

## 清单与迁移记录

2026-10-27 全量原生资源库完成：974 个来源包、116,094 个最终文件、37,356,091,642 字节，589 个最终 FF 均为 461 格式，42 个早期 460 候选保留来源记录但不发布。构建错误与未解决冲突均为 0。原包列表保留了解码所需文件名大小写，不能直接使用提取器规范化后的小写文件名作为 AES nonce 参数。

真实会话验证仅有四个顶层联接；旧图片/松散库 40,873 条和 overlay 542 条记录全部保持原内容。首次完整校验和权限设置耗时约 187 秒，随后启动复用校验记录，资源准备和装配约 6.8 秒；90 秒启动观测无资源错误，`players/config_mp.cfg` 写入并保存成功。UI 验收由用户接手，对局未验收。完整记录见 `verification/client/reports/native-resource-migration.json`。旧库与原始材料暂留，标准启动不再使用旧库。

2026-10-21 将运行补丁从历史证据中拆出，五项补丁的顺序、地址和修改前后字节保持一致。准备工具 `tools/prepare_offline_menu_patches.py` 将最小运行清单输出到本目录，分析输入和报告独立归档；已有输出仍拒绝覆盖。

同日将资源供应及构建直接使用的四份固定数据迁入本目录，文件字节和 SHA-256 保持不变。文件内部历史路径保留原含义，当前读取路径由资源模块指定。诊断工具产生的新报告不直接覆盖这些固定数据。

2026-10-21 从 `client/workspaces/` 迁入，资源和原始清单字节保持不变。基础资源及库内清单中的绝对来源路径、`output_root` 等字段保留生成时含义；加载器按当前项目固定目录定位已部署资源。新构建清单记录新的输出位置，不通过批量改写历史证据实现迁移。
