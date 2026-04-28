[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'AsklumiaLite.Common.ps1')

$paths = Get-AsklumiaLitePaths
$settings = Get-AsklumiaLiteSettings -SettingsPath $paths.SettingsFile
$process = Get-AsklumiaLiteProcess -Paths $paths -Settings $settings

if (-not $process) {
    Write-Host 'Asklumia-lite is not running.'
    exit 0
}

Stop-Process -Id $process.ProcessId -Force
Start-Sleep -Milliseconds 500
if (Test-Path -LiteralPath $paths.PidFile) {
    Remove-MailpitPath -Path $paths.PidFile -AllowedRoot $paths.RuntimeDir
}

Write-Host "Asklumia-lite stopped (PID $($process.ProcessId))."
