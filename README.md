# CODOL 客户端

面向《使命召唤 Online》（CODOL）1649370 版本的离线客户端运行与本地档案适配。

客户端通过 Python 宿主、固定版本原生适配、离线守卫和资源库组织运行环境。当前支持本地离线适配，以及可选的服务端登录、创建房间和退出房间；尚未实现加入他人房间、实时成员同步及完整对局闭环。

## 当前能力与边界

| 方向 | 当前状态 |
| --- | --- |
| 离线启动 | 已有固定版本镜像、启动预检、会话目录装配和临时断网守卫 |
| 角色档案 | 联机配装和成长按玩家保存到服务端。独立离线启动使用临时默认档案 |
| 商城与军火箱 | 购买与开箱使用联机账号服务。充值按钮沿用原版网页入口 |
| 原生 UI | 角色预览、大厅导航和本地房间沿用现有适配。玩家改名需要登录服务器并消耗一张改名卡 |
| 分析工具 | 提供输入版本校验、独立副本、PE 恢复及 Fastfile、资源分析入口 |

已有实现和历史测试通过不等于当前版本已完成实机验收。

联机玩家首次建立仓库时获得初始物品，GP 和 CP 各为 20 亿。角色首次使用原生默认配装，后续登录恢复该玩家最近保存的角色和成长。独立离线退出后丢弃本次配装变更。详情见[客户端架构设计](docs/架构设计.md)。

## 目录结构

```text
client/
├── start_client.cmd        Windows 离线启动入口
├── run.py                  版本预检与离线分析入口
├── requirements.txt        Python 依赖
├── requirements-build.txt  Windows EXE 构建依赖
├── src/codol_rebuild/      客户端生产源码
├── native/                 原生守卫源码
├── tools/                  启动及资源准备工具
├── tests/                  正式自动化测试与固定夹具
├── bin/                    固定运行程序和组件
├── resources/              固定版本运行资源
├── workspaces/             运行会话、缓存和构建中间材料
├── manifests/              输入版本清单
└── docs/                   架构与变更记录
```

客户端源码按 `application`、`adapters`、`resources`、`platform`、`runtime`、`bootstrap`、`analysis` 和 `core` 分层。依赖方向与模块职责以[客户端架构设计](docs/架构设计.md)为准。

## 环境准备

- Windows 客户端使用 Cloud Files 在本地 NTFS 卷上按需提供游戏文件。
- 源码版启动脚本通过 PATH 调用 64 位 Python。以下示例使用本机安装路径。
- 发行资源解压到 `client/`。游戏使用项目内的终版原包、读取组件、固定镜像、守卫和补丁。

在 `client/` 目录打开 PowerShell，安装依赖：

```powershell
& C:\Python314\python.exe -m pip install -r .\requirements.txt
```

运行前确认至少存在以下内容：

```text
bin/v1649370/codol1649370-offline.exe
bin/match_endpoint_guard.exe
manifests/offline-menu-patches.json
bin/v1649370/packed-reader/ifs_reader.exe
bin/v1649370/packed-reader/cloud_files.dll
resources/v1649370/packed/packed-index.json.gz
resources/v1649370/packed/IIPS/IFS2.dll
resources/v1649370/packed/IIPS/IIPSDownload/
```

镜像和补丁会进行固定指纹检查，不可用其他版本文件直接替换。资源来源、准备流程及清单约定见[固定版本运行资源](docs/终版原包资源.md)，镜像说明见[离线运行基底](bin/v1649370/README.md)。

## 构建 Windows EXE 发行包

在 Windows x64 构建机上安装 64 位 Python 3.14 和 Visual Studio 的“使用 C++ 的桌面开发”工作负载（MSVC 和 Windows SDK）。在 `client/` 目录打开 PowerShell，创建构建环境并生成发行目录。

```powershell
& C:\Python314\python.exe -m venv .\workspaces\nuitka-env
& .\workspaces\nuitka-env\Scripts\python.exe -m pip install -r .\requirements-build.txt
& .\workspaces\nuitka-env\Scripts\python.exe .\tools\build_host.py --output ..\outputs\CODOL-Windows-x64
```

`requirements-build.txt` 固定使用 Nuitka 4.3.5。每次构建使用一个尚不存在的输出目录。上述命令生成 `outputs/CODOL-Windows-x64/`，其中包含 GUI 入口 `CODOL.exe`、配置、原生组件和运行库。用户无需安装 Python。

游戏原包单独交付，构建产物不含 `resources/`。部署时把发行目录放到本地 NTFS 卷，再把已准备好的整个 `packed/` 目录放到 `CODOL.exe` 旁的 `resources/v1649370/` 下。部署完成后应能看到 `resources/v1649370/packed/packed-index.json.gz` 和 `resources/v1649370/packed/IIPS/IIPSDownload/`。用户双击 `CODOL.exe`，启动器读取同目录下的 `config/server.ini`。

自动化预检可以从 `client/` 运行下面的 PowerShell 命令。`Start-Process -Wait -PassThru` 等待 GUI 进程退出并返回退出码。`--verify-resources` 会逐个校验已部署的资源。

```powershell
$launcher = (Resolve-Path '..\outputs\CODOL-Windows-x64\CODOL.exe').Path
$process = Start-Process -FilePath $launcher -ArgumentList '--dry-run', '--verify-resources' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "客户端预检失败，退出码 $($process.ExitCode)" }
```

开发时仍可在 `client/` 运行 `start_client.cmd`，直接使用源码和项目内的资源。

发行目录不附带项目的 `.py` 文件。

## 启动器检查更新

从 GitHub Release 单独下载 `CODOLLauncher.exe`，放入目标目录后打开，点击“下载最新版”即可将客户端程序安装到同级目录。准备好游戏原包后点击“开始游戏”。后续打开时会在后台检查更新，也可手动检查或直接启动已安装的游戏。

GitHub 和 R2 都成功发布后，工作流为 GitHub 当前 Latest 版本写入更新清单。安装目录中的 `installed-version.json` 记录已安装版本。启动器本身可通过从新版 ZIP 提取 `CODOLLauncher.exe` 手动升级。

## 自动发布到 GitHub Releases 和 Cloudflare R2

首次启用时，在客户端仓库的 `master` 分支提交工作流和说明，然后推送。下面使用本机的远端名称 `client`。

```powershell
git add .github/workflows/release.yml README.md
git commit -m "添加 Windows 客户端自动发布流程"
git push client master
```

每次推送到 `master`，GitHub Actions 都会检出这次推送的最后一个提交，在 Windows x64 上运行发行测试并构建程序。构建通过后，工作流按进入发布步骤的顺序分配版本号。当前系列从 `v0.1.0` 起步，之后依次发布 `v0.1.1`、`v0.1.2`。旧提交晚于新提交发布时，新提交的版本保留 **Latest** 标记。

日常修订只需推送代码。要开启新系列，在提交中把 `src/codol_rebuild/__init__.py` 的 `__version__` 改为起始版本，例如 `0.2.0` 或 `1.0.0`。该系列的首个 Release 就使用这个版本号。[版本配置](src/codol_rebuild/__init__.py)

版本预留成功后，GitHub Release 和 Cloudflare R2 两个任务并行下载同一份构建产物，各自校验 SHA-256。R2 只将 ZIP 上传到桶内的 `releases/<版本标签>/`。在仓库 Actions Variables 设置 `R2_BUCKET` 和 `R2_ENDPOINT`，在 Actions Secrets 设置 `R2_ACCESS_KEY_ID` 和 `R2_SECRET_ACCESS_KEY`。

遇到临时故障时，在 Actions 中选择 **Re-run failed jobs**。如果工作流已经为该提交预留了版本号，重试会接着发布同一版本。[查看 Actions 运行记录](https://github.com/BeingIsA/Welcome_CODOL_Client/actions/workflows/release.yml)

在 Release 的 **Assets** 下下载 `CODOL-Windows-x64.zip`，可用同页的 `CODOL-Windows-x64.zip.sha256` 核对文件。把 ZIP 解压到本地 NTFS 目录，将游戏原包放到 `CODOL.exe` 旁的 `resources/v1649370/packed/`，再双击 `CODOL.exe`。[下载 Releases](https://github.com/BeingIsA/Welcome_CODOL_Client/releases)

## 从源码启动客户端

以下命令均在 `client/` 目录执行。

### 1. 核验自带资源

```powershell
python -B -X utf8 .\tools\launch_client.py --prepare-only --verify-resources
```

此命令按固定索引核验包文件与包外依赖，准备运行目录后退出。

### 2. 检查启动计划

```powershell
.\start_client.cmd --dry-run
```

`--dry-run` 检查必要文件、镜像指纹、补丁和宿主参数，显示启动计划；不创建运行会话、不启动客户端，也不请求 UAC。它不等同于完整资源校验或实机验收。

### 3. 正式启动

```powershell
.\start_client.cmd
```

也可双击 `start_client.cmd`。启动器从自身目录下的 `resources/v1649370/packed/` 查找资源。游戏首次访问某个资源时，读取组件从本地 IFS 提供该文件。

启动过程中临时断网守卫可能请求 UAC，Python 宿主保持当前用户权限，客户端使用低权限令牌。守卫未就绪时不会恢复客户端执行。

需要使用其他 Python 路径时，可按以下参数直接调用启动工具：

```powershell
& C:\Python314\python.exe -B -X utf8 .\tools\launch_client.py --local-profile --server-config .\config\server.ini
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

先按[主控部署说明](../server/deploy/Ubuntu新主控部署.md)准备 Nomad 和服务端依赖。使用项目的 CODOL-Linux 开发环境时，在另一终端运行 `server/master/start_server.cmd`。服务端使用默认 8080 端口时，可在项目根运行以下命令。

```powershell
.\client\start_client.cmd
```

启动脚本默认连接 `http://127.0.0.1:8080`，可追加 `--server-url` 指定其他地址。游戏读取本地档案和昵称后自动登录；登录成功才激活大厅。原有创建房间、退出房间入口改为调用服务端，响应成功后完成原生界面回调。人数与密码来自游戏房间配置，服务端 `displayName` 是玩家昵称，不是房间标题；目前服务端不保存房间标题、地图或模式。HTTP 在后台线程执行，重复点击不重复提交。直接调用 `tools/launch_client.py` 且未指定 `--server-url` 时保留离线流程。

本机唯一的玩家 ID 保存在 `%APPDATA%/CODOL/data/accounts/identity.json`，重登时客户端带回 `playerId` 并恢复该玩家的档案。删除身份记录后再登录会创建新账号。机器指纹来自 SMBIOS 系统 UUID 的摘要，更换服务器地址时仍使用同一个玩家 ID；房间恢复信息单独保存。

建房请求发送前保存恢复标记。响应丢失时不会自动重放建房；下一次尝试会分页查询自己的房间并清理后重建。正常关闭客户端会尝试离房；失败保留记录，下次登录清理。离房失败会保留原生房间，允许重试。首次登录响应完全丢失时，当前服务端没有幂等登录键，尚不能恢复未收到的 `playerId`。

登录后建立 `/ws?playerId=...` 连接，首次握手成功才激活大厅，服务端据此统计在线人数。后台负责协议保活、断线重连和退出清理；身份被拒绝时停止重连。新增运行依赖 `websockets==15.0.1`，在项目根目录执行 `python -m pip install -r client/requirements.txt` 安装。

`--server-timeout 5` 可调整单次网络超时（0.1～30 秒）；`--server-identity <路径>` 可为验证指定独立身份文件。本期使用 HTTP 与现有 `X-Player-Id` 接口，不增加加密或登录凭据。踢人和关房等实时事件尚未映射到游戏界面；也不支持多进程共用同一个身份文件。

专项验证：`C:\Python314\python.exe -B -X utf8 verification/client/tools/verify_multiplayer.py` 验证真实 HTTP 闭环；加 `--game` 使用隔离账号、设置和临时服务端，验证真实客户端原生回调，不替代手工点击界面验收。

## 档案、资源与日志

- **持久数据**：服务端保存角色档案和仓库。本机的 `%APPDATA%/CODOL/data/accounts/identity.json` 用于找回服务端身份，原生设置位于 `%APPDATA%/CODOL/players/`。
- **会话产物**：每次实际运行在 `workspaces/offline-runs/<会话名>/` 中保存独立程序副本、配置及诊断产物；启动输出会给出对应目录。
- **自带资源**：终版 IFS、包外依赖与索引随客户端放在 `resources/v1649370/packed/`。
- **按需缓存**：游戏访问某个文件时才填充其内容。游戏退出时宿主断开 Cloud Files 连接。下次租用同版本的同一槽位时复用占位文件和已填充内容，玩家设置保存在独立目录。

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
| [固定版本运行资源](docs/终版原包资源.md) | 资源内容、准备流程与清单约定 |
| [离线运行基底](bin/v1649370/README.md) | 固定镜像来源、指纹与使用方式 |
| [客户端自动化测试](tests/README.md) | 测试范围、夹具与环境依赖 |

### 本机游戏服务器配置

`start_client.cmd` 读取 `config/server.ini` 的 `[server] ip/port`（服务器IPv4及HTTP端口）和 `[client] port`（客户端本地UDP端口，默认30001）。本机服务端填127.0.0.1，远程服务端填实际可访问的IPv4；无需填写客户端IP。每次联机启动按到服务器的系统路由选择本机地址，再通过原生2916/2917初始化网络。多客户端同机运行可分别指定不同 `--client-port`（使用配置文件时以文件值为准）。守卫收到本局分配后只放行指定 IPv4 和 UDP 端口，换局时替换规则，离局后撤销。Nomad 模式连接节点地址，本机模式沿用大厅地址。独立离线启动不启用网络。服务端监听0.0.0.0、经127.0.0.1管理实例，不再下发对局IP；客户端始终使用配置的服务器IP，分配响应只提供端口和会话资料。此流程面向直连IPv4，不包含运行中切网恢复、NAT或反向代理适配。

配置只声明网络许可，不自动启动游戏服务端，不生成分配/预订记录，也不代表进图已接通。守卫仍保留进程身份核对和动态规则清理；普通客户端启动需要UAC。已提升的独立验收工具可直接启动守卫，避免重复请求UAC。
