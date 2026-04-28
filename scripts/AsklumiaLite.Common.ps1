Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'Common.ps1')

function Get-AsklumiaLitePaths {
    $root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $runtimeDir = Join-Path $root '.asklumia-lite'

    [pscustomobject]@{
        Root         = $root
        RuntimeDir   = $runtimeDir
        DataDir      = Join-Path $runtimeDir 'data'
        LogDir       = Join-Path $runtimeDir 'logs'
        TempDir      = Join-Path $runtimeDir 'tmp'
        StateFile    = Join-Path $runtimeDir 'data\state.json'
        PidFile      = Join-Path $runtimeDir 'asklumia-lite.pid'
        StdoutLog    = Join-Path $runtimeDir 'logs\asklumia-lite.stdout.log'
        StderrLog    = Join-Path $runtimeDir 'logs\asklumia-lite.stderr.log'
        SettingsFile = Join-Path $root 'asklumia-lite.settings.json'
        ScriptFile   = Join-Path $root 'services\asklumia_lite_server.py'
    }
}

function Ensure-AsklumiaLiteDirectories {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths
    )

    foreach ($directory in @($Paths.RuntimeDir, $Paths.DataDir, $Paths.LogDir, $Paths.TempDir)) {
        if (-not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
    }
}

function Get-AsklumiaLiteSettings {
    param(
        [Parameter(Mandatory)]
        [string]$SettingsPath
    )

    if (-not (Test-Path -LiteralPath $SettingsPath)) {
        throw "Asklumia-lite settings file was not found: $SettingsPath"
    }

    $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
    foreach ($requiredProperty in @('apiListen', 'authListen', 'smtpHost', 'smtpPort', 'allowedEmailDomain')) {
        if ([string]::IsNullOrWhiteSpace([string]$settings.$requiredProperty)) {
            throw "asklumia-lite.settings.json is missing required property '$requiredProperty'."
        }
    }

    return $settings
}

function Get-AsklumiaLiteProcess {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths,

        [pscustomobject]$Settings
    )

    if (-not (Test-Path -LiteralPath $Paths.PidFile)) {
        if ($Settings) {
            return Get-AsklumiaLiteProcessFromPorts -Settings $Settings
        }

        return $null
    }

    $rawPid = (Get-Content -LiteralPath $Paths.PidFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($rawPid)) {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        if ($Settings) {
            return Get-AsklumiaLiteProcessFromPorts -Settings $Settings
        }

        return $null
    }

    $pidValue = 0
    if (-not [int]::TryParse($rawPid, [ref]$pidValue)) {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        if ($Settings) {
            return Get-AsklumiaLiteProcessFromPorts -Settings $Settings
        }

        return $null
    }

    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        if ($Settings) {
            return Get-AsklumiaLiteProcessFromPorts -Settings $Settings
        }

        return $null
    }

    return [pscustomobject]@{
        ProcessId = $process.Id
        Name      = $process.ProcessName
        Source    = 'pidfile'
    }
}

function Get-AsklumiaLiteListeningPids {
    param(
        [Parameter(Mandatory)]
        [int]$Port
    )

    $lines = cmd /c "netstat -ano | findstr :$Port" 2>$null
    if (-not $lines) {
        return @()
    }

    $pids = foreach ($line in $lines) {
        if ($line -match 'LISTENING\s+(\d+)\s*$') {
            [int]$Matches[1]
        }
    }

    return @($pids | Sort-Object -Unique)
}

function Get-AsklumiaLiteProcessFromPorts {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Settings
    )

    $apiPort = (Split-BindAddress -BindAddress $Settings.apiListen).Port
    $authPort = (Split-BindAddress -BindAddress $Settings.authListen).Port
    $apiPids = Get-AsklumiaLiteListeningPids -Port $apiPort
    $authPids = Get-AsklumiaLiteListeningPids -Port $authPort

    if (-not $apiPids -or -not $authPids) {
        return $null
    }

    $commonPid = $apiPids | Where-Object { $authPids -contains $_ } | Select-Object -First 1
    if (-not $commonPid) {
        return $null
    }

    $process = Get-Process -Id $commonPid -ErrorAction SilentlyContinue
    if (-not $process) {
        return $null
    }

    return [pscustomobject]@{
        ProcessId = $process.Id
        Name      = $process.ProcessName
        Source    = 'port-detection'
    }
}

function Initialize-AsklumiaLiteLogs {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths
    )

    foreach ($logFile in @($Paths.StdoutLog, $Paths.StderrLog)) {
        if (Test-Path -LiteralPath $logFile) {
            Clear-Content -LiteralPath $logFile
        } else {
            New-Item -ItemType File -Path $logFile | Out-Null
        }
    }
}

function Wait-ForAsklumiaLiteReady {
    param(
        [Parameter(Mandatory)]
        [string]$ApiBindAddress,

        [Parameter(Mandatory)]
        [string]$AuthBindAddress,

        [int]$TimeoutSeconds = 10
    )

    $apiParts = Split-BindAddress -BindAddress $ApiBindAddress
    $authParts = Split-BindAddress -BindAddress $AuthBindAddress
    $apiHost = Get-LoopbackHost -BindHost $apiParts.Host
    $authHost = Get-LoopbackHost -BindHost $authParts.Host
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([DateTime]::UtcNow -lt $deadline) {
        $apiReady = Test-TcpPort -TargetHost $apiHost -Port $apiParts.Port -TimeoutMs 500
        $authReady = Test-TcpPort -TargetHost $authHost -Port $authParts.Port -TimeoutMs 500

        if ($apiReady -and $authReady) {
            return $true
        }

        Start-Sleep -Milliseconds 300
    }

    return $false
}
