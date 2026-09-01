param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("toggle", "on", "off", "query")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Payload {
    param(
        [hashtable]$Payload,
        [int]$ExitCode
    )

    [Console]::Out.WriteLine(($Payload | ConvertTo-Json -Compress -Depth 5))
    exit $ExitCode
}

function Await-Operation {
    param(
        [object]$Operation,
        [Type]$ResultType,
        [int]$TimeoutMilliseconds = 10000
    )

    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq "AsTask" -and
            $_.IsGenericMethod -and
            $_.GetGenericArguments().Count -eq 1 -and
            $_.GetParameters().Count -eq 1
        } |
        Select-Object -First 1
    if ($null -eq $method) {
        throw "Windows Runtime task bridge is unavailable"
    }
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    if (-not $task.Wait($TimeoutMilliseconds)) {
        throw "Windows Runtime operation timed out"
    }
    return $task.Result
}

function Radio-State {
    param([object[]]$Radios)

    $states = @($Radios | ForEach-Object { $_.State.ToString().ToLowerInvariant() })
    if (@($states | Where-Object { $_ -ne "on" }).Count -eq 0) {
        return "on"
    }
    if (@($states | Where-Object { $_ -ne "off" }).Count -eq 0) {
        return "off"
    }
    if (@($states | Where-Object { $_ -ne "disabled" }).Count -eq 0) {
        return "disabled"
    }
    return "mixed"
}

function Radio-Details {
    param([object[]]$Radios)

    return @($Radios | ForEach-Object {
        @{
            name = $_.Name
            state = $_.State.ToString().ToLowerInvariant()
        }
    })
}

try {
    [void][Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
    [void][Reflection.Assembly]::LoadWithPartialName("System.Runtime.WindowsRuntime")

    $listType = [System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]
    $allRadios = @(Await-Operation ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) $listType)
    $radios = @($allRadios | Where-Object {
        $_.Kind -eq [Windows.Devices.Radios.RadioKind]::Bluetooth
    })
    if ($radios.Count -eq 0) {
        Write-Payload @{ ok = $false; code = "no_radio" } 2
    }

    $before = Radio-State $radios
    if ($Action -eq "query") {
        Write-Payload @{
            ok = $true
            action = $Action
            state = $before
            changed = $false
            method = "winrt"
            radios = @(Radio-Details $radios)
        } 0
    }
    if ($before -eq "disabled") {
        Write-Payload @{ ok = $false; code = "disabled_radio" } 3
    }

    if ($Action -eq "toggle") {
        $target = if ($before -eq "on") { "off" } else { "on" }
    } else {
        $target = $Action
    }
    if ($before -eq $target) {
        Write-Payload @{
            ok = $true
            action = $Action
            state = $before
            changed = $false
            method = "winrt"
            radios = @(Radio-Details $radios)
        } 0
    }

    $accessType = [Windows.Devices.Radios.RadioAccessStatus]
    $access = Await-Operation ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) $accessType
    if ($access -ne [Windows.Devices.Radios.RadioAccessStatus]::Allowed) {
        Write-Payload @{
            ok = $false
            code = "access_denied"
            detail = $access.ToString()
        } 4
    }

    $targetState = if ($target -eq "on") {
        [Windows.Devices.Radios.RadioState]::On
    } else {
        [Windows.Devices.Radios.RadioState]::Off
    }
    foreach ($radio in $radios) {
        if ($radio.State -eq $targetState) {
            continue
        }
        $status = Await-Operation ($radio.SetStateAsync($targetState)) $accessType
        if ($status -ne [Windows.Devices.Radios.RadioAccessStatus]::Allowed) {
            Write-Payload @{
                ok = $false
                code = "state_change_denied"
                detail = $status.ToString()
            } 5
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $pending = @($radios | Where-Object { $_.State -ne $targetState })
        if ($pending.Count -eq 0) {
            Write-Payload @{
                ok = $true
                action = $Action
                state = $target
                changed = $true
                method = "winrt"
                radios = @(Radio-Details $radios)
            } 0
        }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)

    Write-Payload @{
        ok = $false
        code = "state_not_applied"
        detail = (Radio-State $radios)
    } 6
} catch {
    Write-Payload @{
        ok = $false
        code = "winrt_unavailable"
        detail = $_.Exception.Message
    } 7
}
