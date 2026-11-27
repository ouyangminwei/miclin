$ErrorActionPreference = 'Stop'
$taskName = 'Miclin-Taiwan-Starred-2P'
$root = $PSScriptRoot
$python = (Get-Command python.exe).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
$program = Join-Path $root 'monitor.py'
$argument = '"' + $program + '"'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Actions[0].Arguments -ne $argument) {
        throw "Task name already used by a different program: $taskName"
    }
    Get-ScheduledTaskInfo -TaskName $taskName
    return
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argument -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
Get-ScheduledTaskInfo -TaskName $taskName
