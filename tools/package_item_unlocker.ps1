# 在项目根目录运行；仅打包已安装的专用图形工具依赖。
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $projectDirectory
$pythonExecutable = Join-Path $projectDirectory 'verification/client/workspaces/item-unlocker-venv/Scripts/python.exe'
$env:TEMP = Join-Path $projectDirectory 'verification/client/workspaces/item-unlocker-temp'
$env:TMP = $env:TEMP
& $pythonExecutable -m PyInstaller --noconfirm --clean --onefile --windowed --name CODOL_ItemUnlocker --paths client/src --add-data 'client/resources/item_catalog.json;resources' --distpath outputs/item-unlocker --workpath verification/client/workspaces/item-unlocker-build --specpath verification/client/workspaces client/src/codol_rebuild/tools/item_unlocker/main.py
if ($LASTEXITCODE -ne 0) { throw '物品解锁器打包失败' }
