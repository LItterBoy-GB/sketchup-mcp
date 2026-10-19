[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("Install", "Start", "Stop", "Restart", "Status", "RotateToken", "Uninstall", "Run")]
    [string]$Action = "Status",

    [ValidateRange(1, 65535)]
    [int]$HttpPort = 8765,

    [ValidateRange(1, 65535)]
    [int]$SketchUpPort = 9876,

    [switch]$AllowAutostart = $true,

    [ValidateNotNullOrEmpty()]
    [string]$SketchUpExecutable = "C:\Program Files\SketchUp\SketchUp 2022\SketchUp.exe",

    [ValidateRange(1, 3600)]
    [int]$StartupTimeout = 90,

    [ValidateRange(1, 600000)]
    [int]$RequestTimeoutMs = 15000,

    [ValidateRange(1, 86400)]
    [int]$SessionIdleTimeout = 1800,

    [switch]$RemoveToken,

    [switch]$DisableAutostart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:TaskName = "SketchUpMCP HTTP Bridge"
$script:TaskPath = "\"
$script:TokenEnvironmentVariable = "SKETCHUP_MCP_HTTP_TOKEN"
$script:RepositoryRoot = Split-Path -Parent $PSScriptRoot
$script:PythonPath = $null
$script:BootstrapPath = Join-Path $script:RepositoryRoot "scripts\run-http-daemon.pyw"
$script:CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$script:DefaultLogPath = "%LOCALAPPDATA%\SketchUpMCP\logs\http-daemon.log"

function Get-BridgeTask {
    return Get-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Assert-BridgeTaskInstalled {
    if ($null -eq (Get-BridgeTask)) {
        throw "Scheduled task '$script:TaskName' is not installed for the current user. Run Install first."
    }
}

function Get-UserToken {
    return [Environment]::GetEnvironmentVariable($script:TokenEnvironmentVariable, "User")
}

function New-UrlSafeToken {
    # Use 32 random bytes (256 bits), then Base64URL encode without padding.
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }

    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Send-EnvironmentChangeNotification {
    if ($null -eq ("SketchUpMcpEnvironmentChangeNotifier" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class SketchUpMcpEnvironmentChangeNotifier
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(
        IntPtr hWnd,
        uint message,
        IntPtr wParam,
        string lParam,
        uint flags,
        uint timeout,
        out IntPtr result);
}
"@
    }

    $result = [IntPtr]::Zero
    # HWND_BROADCAST + WM_SETTINGCHANGE lets future user processes read the new token.
    [void][SketchUpMcpEnvironmentChangeNotifier]::SendMessageTimeout(
        [IntPtr]0xffff,
        0x001a,
        [IntPtr]::Zero,
        "Environment",
        0x0002,
        5000,
        [ref]$result
    )
}

function Set-UserToken {
    param([Parameter(Mandatory = $true)][string]$Token)

    [Environment]::SetEnvironmentVariable($script:TokenEnvironmentVariable, $Token, "User")
    Send-EnvironmentChangeNotification
}

function Ensure-UserToken {
    if (-not [string]::IsNullOrWhiteSpace((Get-UserToken))) {
        return $false
    }

    Set-UserToken -Token (New-UrlSafeToken)
    return $true
}

function ConvertTo-TaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Contains('"')) {
        throw "Task arguments cannot contain a double quote: $Value"
    }

    if ($Value -notmatch "[\s]") {
        return $Value
    }

    # A trailing backslash must be doubled before the closing quote in a Windows command line.
    $escapedValue = [regex]::Replace($Value, "(\\+)$", '$1$1')
    return '"' + $escapedValue + '"'
}

function Get-BridgePythonArguments {
    $arguments = @(
        "-I",
        "-S",
        $script:BootstrapPath,
        "--http-port",
        $HttpPort.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--sketchup-port",
        $SketchUpPort.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--sketchup-executable",
        $SketchUpExecutable,
        "--startup-timeout",
        $StartupTimeout.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--request-timeout-ms",
        $RequestTimeoutMs.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--session-idle-timeout",
        $SessionIdleTimeout.ToString([Globalization.CultureInfo]::InvariantCulture)
    )

    if ($AllowAutostart -and -not $DisableAutostart) {
        $arguments += "--allow-autostart"
    }

    return $arguments
}

function Get-BridgeTaskArguments {
    $arguments = Get-BridgePythonArguments
    return ($arguments | ForEach-Object { ConvertTo-TaskArgument -Value ([string]$_) }) -join " "
}

function Assert-BridgeLaunchPrerequisites {
    $configPath = Join-Path $script:RepositoryRoot '.venv\pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Missing $configPath. Recreate the virtual environment and install the project first."
    }
    $homeLine = @(Get-Content -LiteralPath $configPath -Encoding UTF8 | Where-Object { $_ -match '^\s*home\s*=' })
    if ($homeLine.Count -ne 1) {
        throw "Expected one base Python home in $configPath."
    }
    $pythonHome = ($homeLine[0] -split '=', 2)[1].Trim()
    if (-not [IO.Path]::IsPathRooted($pythonHome)) {
        throw "Base Python home must be an absolute path in $configPath."
    }
    $script:PythonPath = Join-Path $pythonHome 'pythonw.exe'
    # 不调用 venv 启动器：uv 可能把控制台 shim 命名为 pythonw.exe。
    Assert-GuiPythonExecutable -Path $script:PythonPath
    if (-not (Test-Path -LiteralPath $script:BootstrapPath -PathType Leaf)) {
        throw "Missing HTTP bootstrap: $script:BootstrapPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $script:RepositoryRoot '.venv\Lib\site-packages') -PathType Container)) {
        throw "Missing virtual-environment site-packages. Install the project first."
    }
}

function Assert-GuiPythonExecutable {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Base GUI Python was not found: $Path"
    }
    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ($stream.Length -lt 64 -or $reader.ReadUInt16() -ne 0x5a4d) {
            throw "Invalid Python PE header: $Path"
        }
        $stream.Position = 60
        $peOffset = $reader.ReadUInt32()
        if ([long]$peOffset + 94 -gt $stream.Length) {
            throw "Truncated Python PE header: $Path"
        }
        $stream.Position = $peOffset
        if ($reader.ReadUInt32() -ne 0x4550) { throw "Invalid Python PE signature: $Path" }
        $stream.Position = $peOffset + 24
        if ($reader.ReadUInt16() -notin @(0x10b, 0x20b)) { throw "Invalid Python optional header: $Path" }
        $stream.Position = $peOffset + 24 + 68
        if ($reader.ReadUInt16() -ne 2) {
            throw "Python launcher is not a Windows GUI executable: $Path. Repair the base Python installation."
        }
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function Get-BridgeTaskHttpPort {
    param([Parameter(Mandatory = $true)]$Task)

    if (@($Task.Actions).Count -ne 1 -or $Task.Actions[0].Arguments -notmatch '(?:^|\s)--http-port\s+(\d+)(?:\s|$)') {
        throw "Unrecognized bridge task arguments. Run Install with the intended options."
    }
    $port = [int]$Matches[1]
    if ($port -lt 1 -or $port -gt 65535) { throw "Invalid bridge task HTTP port: $port" }
    return $port
}

function Update-BridgeTaskLauncher {
    param([Parameter(Mandatory = $true)]$Task)

    [void](Get-BridgeTaskHttpPort -Task $Task)
    $oldAction = $Task.Actions[0]
    $arguments = [string]$oldAction.Arguments
    $bootstrapPrefix = '-I -S ' + (ConvertTo-TaskArgument -Value $script:BootstrapPath)
    if ($arguments.StartsWith($bootstrapPrefix + ' ', [StringComparison]::OrdinalIgnoreCase)) {
        $options = $arguments.Substring($bootstrapPrefix.Length)
    }
    elseif ($arguments -match '^\s*-m\s+sketchup_mcp\.http_(?:daemon|server)(?=\s)(.*)$') {
        $options = $Matches[1]
    }
    else {
        throw "Unrecognized bridge task launcher. Run Install with the intended options."
    }
    # 只迁移启动入口，完整保留原任务的端口、超时和 autostart 参数。
    $newArguments = $bootstrapPrefix + $options
    if ($oldAction.Execute -eq $script:PythonPath -and $arguments -ceq $newArguments -and
        $oldAction.WorkingDirectory -eq $script:RepositoryRoot) { return $false }
    $taskAction = New-ScheduledTaskAction -Execute $script:PythonPath -Argument $newArguments -WorkingDirectory $script:RepositoryRoot
    Set-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -Action $taskAction | Out-Null
    return $true
}

function Invoke-BridgeDaemon {
    Assert-BridgeLaunchPrerequisites

    $process = Start-Process -FilePath $script:PythonPath `
        -ArgumentList (Get-BridgeTaskArguments) `
        -WorkingDirectory $script:RepositoryRoot -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "SketchUp MCP HTTP daemon exited with code $($process.ExitCode). See http-launcher.log."
    }
}

function Test-BridgeTaskActiveState {
    param([Parameter(Mandatory = $true)]$Task)

    return [string]$Task.State -in @("Running", "Queued")
}

function Install-BridgeTask {
    Assert-BridgeLaunchPrerequisites

    $tokenCreated = Ensure-UserToken
    $existingTask = Get-BridgeTask
    if ($null -ne $existingTask) {
        Stop-BridgeTask -ForReplacement
        Wait-BridgeTaskStopped
    }

    $taskAction = New-ScheduledTaskAction `
        -Execute $script:PythonPath `
        -Argument (Get-BridgeTaskArguments) `
        -WorkingDirectory $script:RepositoryRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $script:CurrentUser
    $principal = New-ScheduledTaskPrincipal -UserId $script:CurrentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -Hidden `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    $task = New-ScheduledTask -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings

    Register-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -InputObject $task -Force | Out-Null
    if ($tokenCreated) {
        Write-Output "Installed '$script:TaskName' and created a user-scoped HTTP token."
    }
    else {
        Write-Output "Installed '$script:TaskName' using the existing user-scoped HTTP token."
    }

    Start-BridgeTask
}

function Start-BridgeTask {
    Assert-BridgeTaskInstalled
    Assert-BridgeLaunchPrerequisites
    $task = Get-BridgeTask
    $changed = Update-BridgeTaskLauncher -Task $task
    if ($changed -and (Test-BridgeTaskActiveState -Task $task)) {
        Stop-BridgeTask
        Wait-BridgeTaskStopped
    }
    Start-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath
    Write-Output "Started '$script:TaskName'."
}

function Stop-BridgeTask {
    param([switch]$ForReplacement)

    Assert-BridgeTaskInstalled
    $task = Get-BridgeTask
    try {
        $taskPort = Get-BridgeTaskHttpPort -Task $task
    }
    catch {
        if (-not $ForReplacement) { throw }
        # Install 明确替换旧任务；仅清理本仓库、此次安装指定端口的遗留进程。
        $taskPort = $HttpPort
    }
    # 在停止任务前记录父子关系；Windows 虚拟环境启动器退出后可能留下实际解释器。
    $bridgeProcesses = @(Get-BridgeProcesses -Port $taskPort)
    if (Test-BridgeTaskActiveState -Task $task) {
        # The exact task owns the bridge process; do not terminate unrelated Python processes.
        Stop-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath
    }
    foreach ($process in $bridgeProcesses) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)" -ErrorAction SilentlyContinue
        # 防止 PID 被复用；只终止事先确认属于本仓库、指定端口的 bridge。
        if ($null -ne $current -and $current.CreationDate -eq $process.CreationDate -and
            $current.ExecutablePath -eq $process.ExecutablePath -and $current.CommandLine -eq $process.CommandLine) {
            $result = Invoke-CimMethod -InputObject $current -MethodName Terminate
            if ($result.ReturnValue -ne 0) {
                throw "Could not stop bridge process $($process.ProcessId): $($result.ReturnValue)"
            }
        }
    }
    Write-Output "Stopped '$script:TaskName'."
}

function Wait-BridgeTaskStopped {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $task = Get-BridgeTask
        if ($null -eq $task -or -not (Test-BridgeTaskActiveState -Task $task)) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Scheduled task '$script:TaskName' did not stop within 30 seconds."
}

function Restart-BridgeTask {
    # 先验证新解释器和任务参数，再停止仍在运行的服务。
    Assert-BridgeLaunchPrerequisites
    Assert-BridgeTaskInstalled
    [void](Update-BridgeTaskLauncher -Task (Get-BridgeTask))
    Stop-BridgeTask
    Wait-BridgeTaskStopped
    Start-BridgeTask
}

function Get-BridgeProcesses {
    param([int]$Port = $HttpPort)

    $modulePattern = 'sketchup_mcp\.http_(?:daemon|server)'
    $httpPortPattern = [regex]::Escape($Port.ToString([Globalization.CultureInfo]::InvariantCulture))
    $bootstrapPattern = '(?i)(?:^|\s)-S\s+"?' + [regex]::Escape($script:BootstrapPath) + '"?(?=\s|$)'
    $candidates = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object {
            ($_.CommandLine -match "(?i)(?:^|\s)-m\s+$modulePattern(?:\s|$)" -or $_.CommandLine -match $bootstrapPattern) -and
            $_.CommandLine -match "(?i)(?:^|\s)--http-port\s+$httpPortPattern(?:\s|$)"
        })
    $legacyPythonPath = Join-Path $script:RepositoryRoot '.venv\Scripts\python.exe'
    $legacyPythonWPath = Join-Path $script:RepositoryRoot '.venv\Scripts\pythonw.exe'
    $roots = @($candidates | Where-Object {
        $_.ExecutablePath -in @($legacyPythonWPath, $legacyPythonPath) -or $_.CommandLine -match $bootstrapPattern
    })
    # 先返回启动器的直接子进程，再返回启动器；不按名称批量清理 Python。
    $rootIds = @($roots | ForEach-Object { $_.ProcessId })
    @($candidates | Where-Object { $_.ParentProcessId -in $rootIds -and $_.ProcessId -notin $rootIds })
    $roots
}

function Get-BridgeProcessIds {
    param([int]$Port = $HttpPort)
    return @(Get-BridgeProcesses -Port $Port | ForEach-Object { $_.ProcessId })
}

function Get-HealthCheck {
    param([int]$Port = $HttpPort)
    $uri = "http://127.0.0.1:$Port/healthz"
    try {
        $response = Invoke-WebRequest -Uri $uri -Method Get -TimeoutSec 3 -ErrorAction Stop
        return [PSCustomObject]@{
            Status = "healthy"
            Detail = "HTTP $($response.StatusCode)"
        }
    }
    catch {
        return [PSCustomObject]@{
            Status = "unavailable"
            Detail = $_.Exception.Message
        }
    }
}

function Show-BridgeStatus {
    $task = Get-BridgeTask
    $taskInfo = $null
    if ($null -ne $task) {
        $taskInfo = Get-ScheduledTaskInfo -TaskName $script:TaskName -TaskPath $script:TaskPath -ErrorAction SilentlyContinue
    }
    $taskPort = if ($null -ne $task) { Get-BridgeTaskHttpPort -Task $task } else { $HttpPort }
    $health = Get-HealthCheck -Port $taskPort

    [PSCustomObject]@{
        TaskName = $script:TaskName
        TaskInstalled = ($null -ne $task)
        TaskState = if ($null -ne $task) { [string]$task.State } else { "NotInstalled" }
        LastRunTime = if ($null -ne $taskInfo) { $taskInfo.LastRunTime } else { $null }
        NextRunTime = if ($null -ne $taskInfo) { $taskInfo.NextRunTime } else { $null }
        LastTaskResult = if ($null -ne $taskInfo) { $taskInfo.LastTaskResult } else { $null }
        HttpEndpoint = "http://127.0.0.1:$taskPort"
        LogPath = $script:DefaultLogPath
        LauncherLogPath = "%LOCALAPPDATA%\SketchUpMCP\logs\http-launcher.log"
        Health = $health.Status
        HealthDetail = $health.Detail
        Token = if ([string]::IsNullOrWhiteSpace((Get-UserToken))) { "absent" } else { "present" }
        BridgeProcessIds = (Get-BridgeProcessIds -Port $taskPort) -join ", "
    } | Format-List
}

function Rotate-BridgeToken {
    Set-UserToken -Token (New-UrlSafeToken)
    Write-Output "Rotated the user-scoped HTTP token."

    if ($null -ne (Get-BridgeTask)) {
        Restart-BridgeTask
    }
    else {
        Write-Output "The scheduled task is not installed, so no bridge process was restarted."
    }
}

function Uninstall-BridgeTask {
    $task = Get-BridgeTask
    if ($null -ne $task) {
        Stop-BridgeTask
        Wait-BridgeTaskStopped
        Unregister-ScheduledTask -TaskName $script:TaskName -TaskPath $script:TaskPath -Confirm:$false
        Write-Output "Uninstalled '$script:TaskName'. Repository files and logs were preserved."
    }
    else {
        Write-Output "Scheduled task '$script:TaskName' is not installed. Repository files and logs were preserved."
    }

    if ($RemoveToken) {
        # This clears only this user's bridge token; no machine-level variables are touched.
        [Environment]::SetEnvironmentVariable($script:TokenEnvironmentVariable, $null, "User")
        Send-EnvironmentChangeNotification
        Write-Output "Removed the current user's HTTP token."
    }
    else {
        Write-Output "The current user's HTTP token was preserved. Use Uninstall -RemoveToken to remove it."
    }
}

if ($RemoveToken -and $Action -ne "Uninstall") {
    throw "-RemoveToken can only be used with Uninstall."
}

if ($DisableAutostart -and $Action -ne "Run") {
    throw "-DisableAutostart is reserved for the scheduled task runner."
}

switch ($Action) {
    "Install" { Install-BridgeTask }
    "Start" { Start-BridgeTask }
    "Stop" { Stop-BridgeTask }
    "Restart" { Restart-BridgeTask }
    "Status" { Show-BridgeStatus }
    "RotateToken" { Rotate-BridgeToken }
    "Uninstall" { Uninstall-BridgeTask }
    "Run" { Invoke-BridgeDaemon }
}
