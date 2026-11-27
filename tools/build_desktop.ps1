param([string]$Python = 'python', [switch]$Offline, [string]$Wheelhouse)
$ErrorActionPreference='Stop'
$Root=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$OldUserBase=$env:PYTHONUSERBASE
$OldNoUserSite=$env:PYTHONNOUSERSITE
$OldPipConfig=$env:PIP_CONFIG_FILE
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
  $PipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check')
  if ($Offline -or $Wheelhouse) {
    # Local wheels only; ignore configured indexes and remote find-links.
    $env:PIP_CONFIG_FILE = 'nul'
    $PipArgs = @('-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--no-index',
                 '--only-binary', ':all:')
  }
  if ($Wheelhouse) {
    $LocalWheels = (Resolve-Path -LiteralPath $Wheelhouse).ProviderPath
    if (-not (Test-Path -LiteralPath $LocalWheels -PathType Container)) {
      throw 'Wheelhouse 必须是包含全部桌面构建 wheel 的本机目录。'
    }
    $PipArgs += @('--find-links', $LocalWheels)
  }
  $PipArgs += @('-r', (Join-Path $Root 'requirements-desktop.txt'))
  & $Py @PipArgs
  if($LASTEXITCODE){throw '依赖安装失败'}
  & $Py -s -m PyInstaller --noconfirm --onefile --windowed --name XiaoGuang --distpath $Root --workpath (Join-Path $Root '.build\host') --specpath (Join-Path $Root '.build') --paths (Join-Path $Root 'app') --collect-all webview --collect-all pythonnet --hidden-import clr (Join-Path $Root 'desktop.py')
  if($LASTEXITCODE){throw 'EXE 构建失败'}
  Write-Output (Join-Path $Root 'XiaoGuang.exe')
} finally {
  $env:PYTHONUSERBASE=$OldUserBase
  $env:PYTHONNOUSERSITE=$OldNoUserSite
  $env:PIP_CONFIG_FILE=$OldPipConfig
}
