[CmdletBinding()]
param(
    [switch]$ForceDownload
)

. (Join-Path $PSScriptRoot 'Common.ps1')

$paths = Get-MailpitPaths
$settings = Get-MailpitSettings -SettingsPath $paths.SettingsFile
$existingProcess = Get-MailpitProcess -Paths $paths

if ($existingProcess) {
    Write-Host "Mailpit is already running (PID $($existingProcess.Id))."
    Write-Host "UI   : $(Get-MailpitUiUrl -BindAddress $settings.listen)"
    Write-Host "SMTP : $(Get-MailpitSmtpEndpoint -BindAddress $settings.smtp)"
    exit 0
}

Ensure-MailpitDirectories -Paths $paths
Initialize-MailpitLogs -Paths $paths

$installResult = Install-Mailpit -Paths $paths -Force:$ForceDownload
$argumentList = New-MailpitArguments -Settings $settings -Paths $paths
$argumentString = Convert-ToProcessArgumentString -Arguments $argumentList

$process = Start-Process -FilePath $paths.ExePath `
    -ArgumentList $argumentString `
    -WorkingDirectory $paths.Root `
    -RedirectStandardOutput $paths.StdoutLog `
    -RedirectStandardError $paths.StderrLog `
    -WindowStyle Hidden `
    -PassThru

Set-Content -LiteralPath $paths.PidFile -Value $process.Id -NoNewline

if (-not (Wait-ForMailpitReady -UiBindAddress $settings.listen -TimeoutSeconds 10)) {
    Start-Sleep -Seconds 1
    $process.Refresh()

    if ($process.HasExited) {
        Remove-MailpitPath -Path $paths.PidFile -AllowedRoot $paths.RuntimeDir
        $stderrTail = Get-LogTail -Path $paths.StderrLog
        $stdoutTail = Get-LogTail -Path $paths.StdoutLog

        if (-not [string]::IsNullOrWhiteSpace($stderrTail)) {
            throw "Mailpit failed to start.`n`n$stderrTail"
        }

        if (-not [string]::IsNullOrWhiteSpace($stdoutTail)) {
            throw "Mailpit failed to start.`n`n$stdoutTail"
        }

        throw 'Mailpit failed to start for an unknown reason.'
    }
}

$version = if ([string]::IsNullOrWhiteSpace($installResult.tag_name)) { 'unknown' } else { $installResult.tag_name }
Write-Host "Mailpit is running."
Write-Host "Version: $version"
Write-Host "UI   : $(Get-MailpitUiUrl -BindAddress $settings.listen)"
Write-Host "SMTP : $(Get-MailpitSmtpEndpoint -BindAddress $settings.smtp)"
Write-Host "Logs : $($paths.LogDir)"
