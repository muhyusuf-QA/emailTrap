[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'AsklumiaLite.Common.ps1')

$paths = Get-AsklumiaLitePaths
$settings = Get-AsklumiaLiteSettings -SettingsPath $paths.SettingsFile
$existingProcess = Get-AsklumiaLiteProcess -Paths $paths -Settings $settings

if ($existingProcess) {
    Write-Host "Asklumia-lite is already running (PID $($existingProcess.ProcessId))."
    Write-Host "API  : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.apiListen)/health"
    Write-Host "AUTH : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.authListen)/health"
    exit 0
}

Ensure-AsklumiaLiteDirectories -Paths $paths
Initialize-AsklumiaLiteLogs -Paths $paths

$mailpitStartScript = Join-Path $PSScriptRoot 'Start-Mailpit.ps1'
& $mailpitStartScript | Out-Null

$pythonArguments = @(
    $paths.ScriptFile,
    '--config', $paths.SettingsFile,
    '--state-file', $paths.StateFile
)
$argumentString = Convert-ToProcessArgumentString -Arguments $pythonArguments

$process = Start-Process -FilePath 'python' `
    -ArgumentList $argumentString `
    -WorkingDirectory $paths.Root `
    -RedirectStandardOutput $paths.StdoutLog `
    -RedirectStandardError $paths.StderrLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $paths.PidFile -Value $process.Id -NoNewline

if (-not (Wait-ForAsklumiaLiteReady -ApiBindAddress $settings.apiListen -AuthBindAddress $settings.authListen -TimeoutSeconds 10)) {
    Start-Sleep -Seconds 1
    $process.Refresh()

    if ($process.HasExited) {
        Remove-MailpitPath -Path $paths.PidFile -AllowedRoot $paths.RuntimeDir
        $stderrTail = Get-LogTail -Path $paths.StderrLog
        $stdoutTail = Get-LogTail -Path $paths.StdoutLog

        if (-not [string]::IsNullOrWhiteSpace($stderrTail)) {
            throw "Asklumia-lite failed to start.`n`n$stderrTail"
        }

        if (-not [string]::IsNullOrWhiteSpace($stdoutTail)) {
            throw "Asklumia-lite failed to start.`n`n$stdoutTail"
        }

        throw 'Asklumia-lite failed to start for an unknown reason.'
    }
}

Write-Host 'Asklumia-lite is running.'
Write-Host "API  : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.apiListen)"
Write-Host "AUTH : http://$(Get-MailpitSmtpEndpoint -BindAddress $settings.authListen)"
Write-Host "Logs : $($paths.LogDir)"
