param([string]$Python = 'python')
$ErrorActionPreference='Stop'
$Root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$OldUserBase=$env:PYTHONUSERBASE
$OldNoUserSite=$env:PYTHONNOUSERSITE
try {
  $env:PYTHONNOUSERSITE='1'
  $env:PYTHONUSERBASE=Join-Path $Root '.build\empty-userbase'
  New-Item -ItemType Directory -Path $env:PYTHONUSERBASE -Force | Out-Null
  $Build=Join-Path $Root '.build-env'
  if(-not(Test-Path -LiteralPath (Join-Path $Build 'Scripts\python.exe'))){
    & $Python -m venv $Build
    if($LASTEXITCODE){throw '构建环境创建失败'}
  }
  $Py=Join-Path $Build 'Scripts\python.exe'
  & $Py -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements-desktop.txt')
  if($LASTEXITCODE){throw '依赖安装失败'}
  & $Py -s -m PyInstaller --noconfirm --onefile --windowed --name XiaoGuang --distpath $Root --workpath (Join-Path $Root '.build\host') --specpath (Join-Path $Root '.build') --collect-all webview --collect-all pythonnet --hidden-import clr (Join-Path $Root 'desktop.py')
  if($LASTEXITCODE){throw 'EXE 构建失败'}
  Write-Output (Join-Path $Root 'XiaoGuang.exe')
} finally {
  $env:PYTHONUSERBASE=$OldUserBase
  $env:PYTHONNOUSERSITE=$OldNoUserSite
}
