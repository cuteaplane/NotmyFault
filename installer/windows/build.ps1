[CmdletBinding()]
param(
    [string] $Python = "python",
    [switch] $DevelopmentKey,
    [switch] $NonInteractive,
    [string] $Output
)

$ErrorActionPreference = "Stop"
$buildArguments = @((Join-Path $PSScriptRoot "build_installer.py"), "--python", $Python)
if ($DevelopmentKey) { $buildArguments += "--development-key" }
if ($NonInteractive) { $buildArguments += "--non-interactive" }
if ($Output) { $buildArguments += @("--output", $Output) }

& $Python @buildArguments
exit $LASTEXITCODE
