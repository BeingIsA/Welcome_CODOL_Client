# 在项目根目录运行；仅打包已安装的专用图形工具依赖。
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $projectDirectory
$pythonExecutable = Join-Path $projectDirectory 'verification/client/workspaces/item-unlocker-py312/Scripts/python.exe'
$env:TEMP = Join-Path $projectDirectory 'verification/client/workspaces/item-unlocker-temp'
$env:TMP = $env:TEMP
# 限定依赖搜索路径，避免宿主文档工具的同名 ICU/OpenSSL DLL 混入 Qt。
$env:PATH = (Split-Path -Parent $pythonExecutable) + ';' + $env:SystemRoot + '\System32;' + $env:SystemRoot
$resourceData = (Join-Path $projectDirectory 'client/resources/item_catalog.json') + ';resources'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectDirectory 'verification/client/workspaces/item-unlocker-pyinstaller-cache'
& $pythonExecutable -m PyInstaller --noconfirm --clean --onefile --windowed --name CODOL_ItemUnlocker --paths client/src --add-data $resourceData --distpath outputs/item-unlocker --workpath verification/client/workspaces/item-unlocker-build --specpath verification/client/workspaces client/src/codol_rebuild/tools/item_unlocker/main.py
if ($LASTEXITCODE -ne 0) { throw '物品解锁器打包失败' }
