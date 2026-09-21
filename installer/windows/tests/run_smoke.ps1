[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$WorkDirectory,
    [switch]$MaintenanceOnly
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
$framework = Split-Path -Parent $compiler
$references = @("/reference:$installerPath", "/reference:$framework\System.Xaml.dll")
$references += @('PresentationCore', 'PresentationFramework', 'WindowsBase') | ForEach-Object { "/reference:$framework\WPF\$_.dll" }
& $compiler /nologo /target:exe /platform:x64 /optimize+ /utf8output @references "/out:$testExecutable" (Join-Path $PSScriptRoot 'Smoke.cs')
if ($LASTEXITCODE -ne 0) { throw "测试程序编译失败，退出码 $LASTEXITCODE。" }
$testArguments = @($installerPath, (Join-Path $testDirectory 'work'))
if ($MaintenanceOnly) { $testArguments += '--maintenance-only' }
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $testExecutable
$startInfo.Arguments = ($testArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
try {
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $code = $process.ExitCode
    $text = $stdout.GetAwaiter().GetResult() + $stderr.GetAwaiter().GetResult()
    [System.IO.File]::WriteAllText((Join-Path $testDirectory 'smoke.log'), $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output $text.TrimEnd()
} finally {
    $process.Dispose()
}
if ($code -ne 0) { throw "安装器测试失败，退出码 $code。" }
