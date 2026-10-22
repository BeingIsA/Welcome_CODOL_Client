# 客户端自动化测试

本目录属于正式工程，维护单元、集成、回归及架构测试。测试源码与 `client/src/` 生产源码分离，生产代码不得依赖本目录。

在项目根运行：

```powershell
& C:\Python314\python.exe -B -X utf8 .\client\test.py --output .\verification\client\reports\tests-next.json
```

输出文件必须尚不存在。不指定 `--output` 时只输出控制台结果。入口配置生产模块搜索路径、禁用字节码写入，并将默认临时目录设置为 `verification/client/workspaces/test-temp/`。

依赖见 `client/requirements.txt`。部分测试需要 Windows API、低权限令牌、既有原生镜像、资源与捕获数据；部分 UI 和诊断工具回归使用 `research/` 或 `verification/client/tools/` 中的工具。当前套件不是仅靠合成数据即可在任意环境运行的纯单元测试，不启动游戏，也不代表实机功能验收。

`fixtures/refactor-20261020/` 是冻结的历史源码，用于差分比较；manifest 中的路径和哈希保留历史含义。测试生成报告与临时文件继续放在验证工作区。新增稳定夹具随测试维护，避免继续增加对临时研究产物的依赖。

本次迁移从 `verification/client/tests/` 搬入 39 个测试文件，并将测试入口从 `verification/client/test.py` 移到 `client/test.py`。旧位置的历史字节码与报告不作为测试入口。
