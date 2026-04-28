Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-MailpitPaths {
    $root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $runtimeDir = Join-Path $root '.mailpit'

    [pscustomobject]@{
        Root        = $root
        RuntimeDir  = $runtimeDir
        BinDir      = Join-Path $runtimeDir 'bin'
        DataDir     = Join-Path $runtimeDir 'data'
        LogDir      = Join-Path $runtimeDir 'logs'
        TempDir     = Join-Path $runtimeDir 'tmp'
        ExePath     = Join-Path $runtimeDir 'bin\mailpit.exe'
        VersionFile = Join-Path $runtimeDir 'bin\version.txt'
        PidFile     = Join-Path $runtimeDir 'mailpit.pid'
        StdoutLog   = Join-Path $runtimeDir 'logs\mailpit.stdout.log'
        StderrLog   = Join-Path $runtimeDir 'logs\mailpit.stderr.log'
        SettingsFile = Join-Path $root 'mailpit.settings.json'
    }
}

function Ensure-MailpitDirectories {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths
    )

    foreach ($directory in @($Paths.RuntimeDir, $Paths.BinDir, $Paths.DataDir, $Paths.LogDir, $Paths.TempDir)) {
        if (-not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
    }
}

function Resolve-WorkspacePath {
    param(
        [Parameter(Mandatory)]
        [string]$Root,

        [Parameter(Mandatory)]
        [string]$Path
    )

    if ([System.IO.Path]::IsPathRooted($Path)) {
        return $Path
    }

    return Join-Path $Root $Path
}

function Remove-MailpitPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$AllowedRoot
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $resolvedPath = (Resolve-Path -LiteralPath $Path).Path
    $resolvedRoot = (Resolve-Path -LiteralPath $AllowedRoot).Path

    if (-not $resolvedPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside runtime directory: $resolvedPath"
    }

    Remove-Item -LiteralPath $resolvedPath -Recurse -Force
}

function Get-MailpitSettings {
    param(
        [Parameter(Mandatory)]
        [string]$SettingsPath
    )

    if (-not (Test-Path -LiteralPath $SettingsPath)) {
        throw "Mailpit settings file was not found: $SettingsPath"
    }

    $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json

    foreach ($requiredProperty in @('listen', 'smtp', 'database')) {
        if ([string]::IsNullOrWhiteSpace($settings.$requiredProperty)) {
            throw "mailpit.settings.json is missing required property '$requiredProperty'."
        }
    }

    if (-not ($settings.PSObject.Properties.Name -contains 'maxMessages')) {
        $settings | Add-Member -NotePropertyName maxMessages -NotePropertyValue 500
    }

    foreach ($property in @('label', 'disableVersionCheck', 'disableWal', 'verbose', 'quiet', 'smtpAllowedRecipients', 'smtpIgnoreRejectedRecipients')) {
        if (-not ($settings.PSObject.Properties.Name -contains $property)) {
            switch ($property) {
                'label' { $settings | Add-Member -NotePropertyName label -NotePropertyValue 'LocalMailpit' }
                default { $settings | Add-Member -NotePropertyName $property -NotePropertyValue $false }
            }
        }
    }

    return $settings
}

function Get-MailpitReleaseArchitecture {
    $architecture = $env:PROCESSOR_ARCHITEW6432
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = $env:PROCESSOR_ARCHITECTURE
    }

    $normalized = $architecture.ToUpperInvariant()

    if ($normalized.Contains('ARM64')) {
        return 'arm64'
    }

    if ($normalized.Contains('AMD64') -or $normalized.Contains('X64')) {
        return 'amd64'
    }

    throw "Unsupported Windows architecture for Mailpit: $architecture"
}

function Get-LatestMailpitRelease {
    $headers = @{
        'User-Agent' = 'mailpit-service-bootstrap'
        'Accept'     = 'application/vnd.github+json'
    }

    return Invoke-RestMethod -Uri 'https://api.github.com/repos/axllent/mailpit/releases/latest' -Headers $headers
}

function Install-Mailpit {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths,

        [switch]$Force
    )

    Ensure-MailpitDirectories -Paths $Paths

    if (-not $Force -and (Test-Path -LiteralPath $Paths.ExePath) -and (Test-Path -LiteralPath $Paths.VersionFile)) {
        return [pscustomobject]@{
            tag_name = (Get-Content -LiteralPath $Paths.VersionFile -Raw).Trim()
            installed = $true
            downloaded = $false
        }
    }

    $release = Get-LatestMailpitRelease
    $arch = Get-MailpitReleaseArchitecture
    $assetName = "mailpit-windows-$arch.zip"
    $asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1

    if (-not $asset) {
        throw "Unable to find a Mailpit release asset named '$assetName'."
    }

    $archivePath = Join-Path $Paths.TempDir $asset.name
    $extractPath = Join-Path $Paths.TempDir ("extract-" + $release.tag_name + "-" + $arch)

    Remove-MailpitPath -Path $extractPath -AllowedRoot $Paths.RuntimeDir

    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force

    $binary = Get-ChildItem -LiteralPath $extractPath -Recurse -Filter 'mailpit.exe' | Select-Object -First 1
    if (-not $binary) {
        throw "The Mailpit archive did not contain mailpit.exe."
    }

    Copy-Item -LiteralPath $binary.FullName -Destination $Paths.ExePath -Force
    Set-Content -LiteralPath $Paths.VersionFile -Value $release.tag_name -NoNewline

    Remove-MailpitPath -Path $extractPath -AllowedRoot $Paths.RuntimeDir
    Remove-MailpitPath -Path $archivePath -AllowedRoot $Paths.RuntimeDir

    return [pscustomobject]@{
        tag_name = $release.tag_name
        installed = $true
        downloaded = $true
    }
}

function Split-BindAddress {
    param(
        [Parameter(Mandatory)]
        [string]$BindAddress
    )

    if ($BindAddress -match '^\[(.+)\]:(\d+)$') {
        return [pscustomobject]@{
            Host = $Matches[1]
            Port = [int]$Matches[2]
        }
    }

    if ($BindAddress -match '^(.*):(\d+)$') {
        return [pscustomobject]@{
            Host = $Matches[1]
            Port = [int]$Matches[2]
        }
    }

    throw "Unable to parse bind address '$BindAddress'. Expected format host:port."
}

function Get-LoopbackHost {
    param(
        [Parameter(Mandatory)]
        [string]$BindHost
    )

    if ([string]::IsNullOrWhiteSpace($BindHost) -or $BindHost -eq '0.0.0.0' -or $BindHost -eq '::' -or $BindHost -eq '*') {
        return '127.0.0.1'
    }

    return $BindHost.Trim('[', ']')
}

function Get-MailpitUiUrl {
    param(
        [Parameter(Mandatory)]
        [string]$BindAddress
    )

    $parts = Split-BindAddress -BindAddress $BindAddress
    $connectHost = Get-LoopbackHost -BindHost $parts.Host
    return "http://${connectHost}:$($parts.Port)"
}

function Get-MailpitSmtpEndpoint {
    param(
        [Parameter(Mandatory)]
        [string]$BindAddress
    )

    $parts = Split-BindAddress -BindAddress $BindAddress
    $connectHost = Get-LoopbackHost -BindHost $parts.Host
    return "${connectHost}:$($parts.Port)"
}

function New-MailpitArguments {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Settings,

        [Parameter(Mandatory)]
        [pscustomobject]$Paths
    )

    $arguments = @(
        '--listen', $Settings.listen,
        '--smtp', $Settings.smtp,
        '--database', (Resolve-WorkspacePath -Root $Paths.Root -Path $Settings.database)
    )

    if (-not [string]::IsNullOrWhiteSpace($Settings.smtpAllowedRecipients)) {
        $arguments += @('--smtp-allowed-recipients', $Settings.smtpAllowedRecipients)
    }

    if ($Settings.smtpIgnoreRejectedRecipients) {
        $arguments += '--smtp-ignore-rejected-recipients'
    }

    if ($null -ne $Settings.maxMessages) {
        $arguments += @('--max', [string]$Settings.maxMessages)
    }

    if (-not [string]::IsNullOrWhiteSpace($Settings.label)) {
        $arguments += @('--label', $Settings.label)
    }

    if ($Settings.disableVersionCheck) {
        $arguments += '--disable-version-check'
    }

    if ($Settings.disableWal) {
        $arguments += '--disable-wal'
    }

    if ($Settings.verbose) {
        $arguments += '--verbose'
    }

    if ($Settings.quiet) {
        $arguments += '--quiet'
    }

    return $arguments
}

function Convert-ToProcessArgumentString {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $escapedArguments = foreach ($argument in $Arguments) {
        if ([string]::IsNullOrEmpty($argument)) {
            '""'
            continue
        }

        if ($argument -notmatch '[\s"]') {
            $argument
            continue
        }

        $escaped = $argument -replace '(\\*)"', '$1$1\"'
        $escaped = $escaped -replace '(\\+)$', '$1$1'
        '"' + $escaped + '"'
    }

    return [string]::Join(' ', $escapedArguments)
}

function Get-MailpitProcess {
    param(
        [Parameter(Mandatory)]
        [pscustomobject]$Paths
    )

    if (-not (Test-Path -LiteralPath $Paths.PidFile)) {
        return $null
    }

    $rawPid = (Get-Content -LiteralPath $Paths.PidFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($rawPid)) {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        return $null
    }

    $pidValue = 0
    if (-not [int]::TryParse($rawPid, [ref]$pidValue)) {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        return $null
    }

    try {
        $process = Get-Process -Id $pidValue -ErrorAction Stop
    } catch {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        return $null
    }

    if ($process.ProcessName -ne 'mailpit') {
        Remove-MailpitPath -Path $Paths.PidFile -AllowedRoot $Paths.RuntimeDir
        return $null
    }

    return $process
}

function Initialize-MailpitLogs {
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

function Test-TcpPort {
    param(
        [Parameter(Mandatory)]
        [string]$TargetHost,

        [Parameter(Mandatory)]
        [int]$Port,

        [int]$TimeoutMs = 1000
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $asyncResult = $null

    try {
        $asyncResult = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return $false
        }

        $client.EndConnect($asyncResult) | Out-Null
        return $true
    } catch {
        return $false
    } finally {
        if ($asyncResult) {
            $asyncResult.AsyncWaitHandle.Close()
        }
        $client.Close()
    }
}

function Wait-ForMailpitReady {
    param(
        [Parameter(Mandatory)]
        [string]$UiBindAddress,

        [int]$TimeoutSeconds = 10
    )

    $parts = Split-BindAddress -BindAddress $UiBindAddress
    $connectHost = Get-LoopbackHost -BindHost $parts.Host
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)

    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-TcpPort -TargetHost $connectHost -Port $parts.Port -TimeoutMs 500) {
            return $true
        }

        Start-Sleep -Milliseconds 300
    }

    return $false
}

function Get-LogTail {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [int]$Lines = 20
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return ''
    }

    return (Get-Content -LiteralPath $Path -Tail $Lines) -join [Environment]::NewLine
}
