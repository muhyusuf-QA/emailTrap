[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'Common.ps1')

$paths = Get-MailpitPaths
$process = Get-MailpitProcess -Paths $paths

if (-not $process) {
    Write-Host 'Mailpit is not running.'
    exit 0
}

Stop-Process -Id $process.Id -Force
Start-Sleep -Milliseconds 500
Remove-MailpitPath -Path $paths.PidFile -AllowedRoot $paths.RuntimeDir

Write-Host "Mailpit stopped (PID $($process.Id))."
