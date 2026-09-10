[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$WorkDirectory
)

$ErrorActionPreference = 'Stop'
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$testDirectory = [System.IO.Path]::GetFullPath($WorkDirectory)
if (Test-Path -LiteralPath $testDirectory) {
    if (Get-ChildItem -LiteralPath $testDirectory -Force | Select-Object -First 1) {
        throw '测试目录必须为空，请指定新的 WorkDirectory。'
    }
} else {
    New-Item -ItemType Directory -Path $testDirectory -Force | Out-Null
}

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$testExecutable = Join-Path $testDirectory 'InstallerSmoke.exe'
& $compiler /nologo /target:exe /platform:x64 /optimize+ /utf8output "/reference:$installerPath" "/out:$testExecutable" (Join-Path $PSScriptRoot 'Smoke.cs')
if ($LASTEXITCODE -ne 0) { throw "测试程序编译失败，退出码 $LASTEXITCODE。" }
& $testExecutable $installerPath (Join-Path $testDirectory 'work') 2>&1 | Tee-Object -FilePath (Join-Path $testDirectory 'smoke.log')
if ($LASTEXITCODE -ne 0) { throw "安装器测试失败，退出码 $LASTEXITCODE。" }
