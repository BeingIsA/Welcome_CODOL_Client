# CODOL 客户端

面向《使命召唤 Online》（CODOL）1649370 版本的离线客户端运行与本地档案适配。

客户端通过 Python 宿主、固定版本原生适配、离线守卫和资源库组织运行环境。当前支持本地离线适配，以及可选的服务端登录、创建房间和退出房间；尚未实现加入他人房间、实时成员同步及完整对局闭环。

## 当前能力与边界

| 方向 | 当前状态 |
| --- | --- |
| 离线启动 | 已有固定版本镜像、启动预检、会话目录装配和临时断网守卫 |
| 本地账号 | 已有 SQLite 档案持久化、昵称、配装及游戏设置保存逻辑 |
| 本地商城 | 已有 GP/CP 购买、单品及礼包、库存与余额事务保存逻辑 |
| 原生 UI | 已有角色预览、大厅导航、本地房间及改名等适配，具体验收状态见变更记录 |
| 分析工具 | 提供输入版本校验、独立副本、PE 恢复及 Fastfile、资源分析入口 |

已有实现和历史测试通过不等于当前版本已完成实机验收。

新建账号使用当前初始化策略：保留满级及进度条件，按商城及排位赛荣誉渠道筛选初始授权；GP 初始值为 20 亿，CP 为 10 万。已有的历史全解锁账号保留原行为。具体授权、数量及购买规则见[客户端架构设计](docs/架构设计.md)；启动脚本中的 “Full-Unlock” 字样不代表所有新账号无条件全解锁。

## 目录结构

```text
client/
├── start_client.cmd        Windows 离线启动入口
├── run.py                  版本预检与离线分析入口
├── requirements.txt        Python 依赖
├── src/codol_rebuild/      客户端生产源码
├── native/                 原生守卫源码
├── tools/                  启动及资源准备工具
├── tests/                  正式自动化测试与固定夹具
├── bin/                    固定运行程序和组件
├── resources/              固定版本运行资源
├── data/accounts/          本地账号、档案与设置
├── workspaces/             运行会话、缓存和构建中间材料
├── manifests/              输入版本清单
├── licenses/               组件许可材料
└── docs/                   架构与变更记录
```

客户端源码按 `application`、`adapters`、`resources`、`platform`、`runtime`、`bootstrap`、`analysis` 和 `core` 分层。依赖方向与模块职责以[客户端架构设计](docs/架构设计.md)为准。

## 环境准备

- **系统**：Windows。实际运行依赖 Win32 API、低权限令牌、目录联接及原生组件。
- **Python**：现有启动脚本固定使用 `C:\Python314\python.exe`。以下命令沿用此路径；使用其他安装路径时，可直接调用 Python 启动入口。
- **输入与资源**：需要匹配 1649370 的输入文件、固定离线镜像、守卫、补丁记录及已准备好的资源库。仅有 Python 源码和依赖不能完成启动。

在 `client/` 目录打开 PowerShell，安装依赖：

```powershell
& C:\Python314\python.exe -m pip install -r .\requirements.txt
```

运行前确认至少存在以下内容：

```text
bin/v1649370/codol1649370-offline.exe
bin/offline_guard.exe
resources/v1649370/offline-menu-patches.json
resources/v1649370/offline-resources/
resources/v1649370/offline-image-library/
resources/v1649370/offline-loose-library/
```

镜像和补丁会进行固定指纹检查，不可用其他版本文件直接替换。资源来源、准备流程及清单约定见[固定版本运行资源](resources/v1649370/README.md)，镜像说明见[离线运行基底](bin/v1649370/README.md)。

## 启动客户端

以下命令均在 `client/` 目录执行。

### 1. 校验输入版本

```powershell
& C:\Python314\python.exe -B -X utf8 .\run.py check
```

此命令按 `manifests/build1649370.json` 校验关键输入，不启动游戏。

### 2. 检查启动计划

```powershell
.\start_client.cmd --dry-run
```

`--dry-run` 检查必要文件、镜像指纹、补丁和宿主参数，显示启动计划；不创建运行会话、不启动客户端，也不请求 UAC。它不等同于完整资源校验或实机验收。

### 3. 正式启动

```powershell
.\start_client.cmd
```

也可双击 `start_client.cmd`。脚本默认启用本地档案，并使用固定图片库与松散资源库，运行到关闭客户端或发生致命错误。

启动过程中临时断网守卫可能请求 UAC，Python 宿主保持当前用户权限，客户端使用低权限令牌。守卫未就绪时不会恢复客户端执行。

需要使用其他 Python 路径时，可按以下参数直接调用启动工具：

```powershell
& C:\Python314\python.exe -B -X utf8 .\tools\launch_client.py `
  --resource-library .\resources\v1649370\offline-image-library `
  --loose-library .\resources\v1649370\offline-loose-library `
  --local-profile
```

常用参数可追加到启动脚本后：

| 参数 | 用途 |
| --- | --- |
| `--dry-run` | 仅检查并显示启动计划 |
| `--prepare-only` | 准备运行目录但不运行游戏；会产生准备产物 |
| `--verify-resources` | 强制对资源进行 SHA-256 全量复核 |
| `--seconds 60` | 执行有时限的诊断运行，允许范围为 1～600 秒 |

完整参数说明：

```powershell
& C:\Python314\python.exe -B -X utf8 .\tools\launch_client.py --help
```

## 服务端登录与房间

先在另一终端运行 `server/start_server.cmd`（首次按脚本提示准备服务端配置）。服务端使用默认的本机 8080 端口时，在项目根运行：

```powershell
.\client\start_client.cmd
```

启动脚本默认连接 `http://127.0.0.1:8080`，可追加 `--server-url` 指定其他地址。游戏读取本地档案和昵称后自动登录；登录成功才激活大厅。原有创建房间、退出房间入口改为调用服务端，响应成功后完成原生界面回调。人数与密码来自游戏房间配置，服务端 `displayName` 是玩家昵称，不是房间标题；目前服务端不保存房间标题、地图或模式。HTTP 在后台线程执行，重复点击不重复提交。直接调用 `tools/launch_client.py` 且未指定 `--server-url` 时保留离线流程。

身份按规范化的服务器地址明文保存在 `data/accounts/server-identities.json`，重登带回 `playerId`。不要为了重试网络错误删除此文件；机器码不负责复用账号，删除身份会导致新建账号。机器指纹来自 SMBIOS 系统 UUID 的摘要，无法获取有效 UUID 时明确拒绝启动联机模式。服务器更换域名、协议或端口会对应不同的身份记录。

建房请求发送前保存恢复标记。响应丢失时不会自动重放建房；下一次尝试会分页查询自己的房间并清理后重建。正常关闭客户端会尝试离房；失败保留记录，下次登录清理。离房失败会保留原生房间，允许重试。首次登录响应完全丢失时，当前服务端没有幂等登录键，尚不能恢复未收到的 `playerId`。

登录后建立 `/ws?playerId=...` 连接，首次握手成功才激活大厅，服务端据此统计在线人数。后台负责协议保活、断线重连和退出清理；身份被拒绝时停止重连。新增运行依赖 `websockets==15.0.1`，在项目根目录执行 `python -m pip install -r client/requirements.txt` 安装。

`--server-timeout 5` 可调整单次网络超时（0.1～30 秒）；`--server-identity <路径>` 可为验证指定独立身份文件。本期使用 HTTP 与现有 `X-Player-Id` 接口，不增加加密或登录凭据。踢人和关房等实时事件尚未映射到游戏界面；也不支持多进程共用同一个身份文件。

专项验证：`C:\Python314\python.exe -B -X utf8 verification/client/tools/verify_multiplayer.py` 验证真实 HTTP 闭环；加 `--game` 使用隔离账号、设置和临时服务端，验证真实客户端原生回调，不替代手工点击界面验收。

## 档案、资源与日志

- **持久数据**：`data/accounts/` 保存账号、档案和游戏设置。备份本地进度时应保留此目录。
- **会话产物**：每次实际运行在 `workspaces/offline-runs/<会话名>/` 中保存独立程序副本、配置及诊断产物；启动输出会给出对应目录。
- **固定资源**：`resources/v1649370/` 提供基础覆盖层、图片库和模型音频等松散资源库。
- **直接共享资源**：普通启动校验并保护固定资源库，通过目录联接直接访问，不再复制整库；`workspaces/resource-library-verification/` 仅保存校验记录。运行期间不要替换源资源库。诊断补件模式仍使用 `workspaces/offline-resource-cache/`，旧缓存清理前须考虑已有会话引用。

不要直接修改固定离线镜像。宿主会先将其复制到会话目录，再应用运行补丁。`workspaces/` 还包含资源输入副本和提取中间材料，不能整体当作可丢弃缓存；清理会话时也不能递归穿过资源目录联接。详见[工作目录说明](workspaces/README.md)。

## 自动化测试

正式测试及固定夹具位于 `tests/`，覆盖账号、档案、资源、原生适配及架构边界等内容。

在客户端目录运行正式测试入口：

```powershell
& C:\Python314\python.exe -B -X utf8 .\test.py
```

可使用 `--output <新报告路径>` 保存结果；报告与临时产物放在 `verification/client/`。测试组织与环境依赖见[客户端自动化测试](tests/README.md)。

部分测试依赖 Windows API、原生镜像、资源和捕获数据，不能视为任意环境下均可运行的纯单元测试。历史测试通过不代表当前版本已重新通过验证，也不代表实机功能验收完成。

## 离线分析

`run.py` 是分析入口，游戏启动使用前述启动脚本。可通过以下命令查看各专题工具：

```powershell
& C:\Python314\python.exe -B -X utf8 .\run.py --help
& C:\Python314\python.exe -B -X utf8 .\run.py fastfile --help
& C:\Python314\python.exe -B -X utf8 .\run.py bootstrap --help
& C:\Python314\python.exe -B -X utf8 .\run.py assets --help
```

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [客户端架构设计](docs/架构设计.md) | 模块边界、账号策略、运行装配与依赖方向 |
| [固定版本运行资源](resources/v1649370/README.md) | 资源内容、准备流程与清单约定 |
| [离线运行基底](bin/v1649370/README.md) | 固定镜像来源、指纹与使用方式 |
| [客户端自动化测试](tests/README.md) | 测试范围、夹具与环境依赖 |
