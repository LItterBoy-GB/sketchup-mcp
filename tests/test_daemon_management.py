import shutil
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "manage-http-daemon.ps1"


class DaemonManagementTests(unittest.TestCase):
    def run_script(self, body):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not installed")
        source = SCRIPT_PATH.read_text(encoding="utf-8").split('switch ($Action)', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "management-test.ps1"
            probe.write_text(source + "\n" + body, encoding="utf-8-sig")
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(probe)],
                                    capture_output=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    @unittest.skipUnless(os.name == "nt", "Requires Windows PE executables")
    def test_preflight_ignores_broken_venv_launcher_and_rejects_console_base(self):
        base = Path(sys.base_prefix)
        with tempfile.TemporaryDirectory(prefix="MCP launcher 中文 ") as directory:
            root = Path(directory)
            (root / ".venv/Lib/site-packages").mkdir(parents=True)
            (root / ".venv/Scripts").mkdir()
            (root / ".venv/Scripts/pythonw.exe").write_bytes(b"broken console shim")
            (root / ".venv/pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
            (root / "bootstrap.pyw").touch()
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            self.run_script(f"""
$script:RepositoryRoot = {quote(root)}
$script:BootstrapPath = {quote(root / 'bootstrap.pyw')}
Assert-BridgeLaunchPrerequisites
if ($script:PythonPath -ne {quote(base / 'pythonw.exe')}) {{ throw 'Did not select base GUI runtime' }}
$rejected = $false
try {{ Assert-GuiPythonExecutable -Path {quote(base / 'python.exe')} }} catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'Accepted console PE as GUI' }}
$rejected = $false
try {{ Assert-GuiPythonExecutable -Path {quote(root / '.venv/Scripts/pythonw.exe')} }} catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'Accepted malformed PE' }}
Set-Content -LiteralPath {quote(root / '.venv/pyvenv.cfg')} -Value 'home = relative-runtime'
$rejected = $false
try {{ Assert-BridgeLaunchPrerequisites }} catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'Accepted relative base home' }}
""")

    def test_start_migrates_running_task_preserving_all_service_options(self):
        self.run_script(r'''
$script:PythonPath = 'C:\base Python\pythonw.exe'
$script:BootstrapPath = 'H:\bridge with spaces\scripts\run-http-daemon.pyw'
$script:RepositoryRoot = 'H:\bridge with spaces'
$script:events = @()
$script:options = ' --http-port 18765 --sketchup-port 9878 --request-timeout-ms 42000 --session-idle-timeout 123 --startup-timeout 77 --sketchup-executable "C:\Custom SU\SketchUp.exe"'
$script:task = [pscustomobject]@{State='Running'; Actions=@([pscustomobject]@{
    Execute='H:\bridge with spaces\.venv\Scripts\pythonw.exe'; Arguments=('-m sketchup_mcp.http_daemon' + $script:options); WorkingDirectory=$script:RepositoryRoot
})}
function Get-BridgeTask { $script:task }
function Assert-BridgeLaunchPrerequisites { $script:events += 'validate' }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) return [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
function Set-ScheduledTask {
    param($TaskName,$TaskPath,$Action)
    $script:events += 'migrate'
    $script:task.Actions = @($Action)
}
function Stop-BridgeTask { $script:events += 'stop'; $script:task.State='Ready' }
function Wait-BridgeTaskStopped { $script:events += 'wait' }
function Start-ScheduledTask { param($TaskName,$TaskPath) $script:events += 'start' }
Start-BridgeTask
if (($script:events -join ',') -ne 'validate,migrate,stop,wait,start') { throw "Wrong lifecycle: $script:events" }
$expected = '-I -S "H:\bridge with spaces\scripts\run-http-daemon.pyw"' + $script:options
if ($script:task.Actions[0].Arguments -cne $expected -or $script:task.Actions[0].Execute -ne $script:PythonPath) { throw 'Changed service options during migration' }
if ((Get-BridgeTaskHttpPort -Task $script:task) -ne 18765) { throw 'Ignored installed HTTP port' }
$script:events = @()
Start-BridgeTask
if (($script:events -join ',') -ne 'validate,start') { throw 'Repeated Start unexpectedly migrated/restarted the task' }
$script:PythonPath = 'C:\new base Python\pythonw.exe'
$script:events = @()
Restart-BridgeTask
if (($script:events -join ',') -ne 'validate,migrate,stop,wait,validate,start') { throw 'Changed base runtime was not validated before stop' }
''')

    def test_invalid_runtime_or_unknown_action_does_not_stop_working_daemon(self):
        self.run_script(r'''
$script:events = @()
$script:task = [pscustomobject]@{State='Running'; Actions=@([pscustomobject]@{Execute='unknown.exe';Arguments='--http-port 18765';WorkingDirectory='H:\other'})}
function Get-BridgeTask { $script:task }
function Stop-BridgeTask { throw 'Must not stop service on preflight failure' }
function Start-ScheduledTask { throw 'Must not start unvalidated launcher' }
function Assert-BridgeLaunchPrerequisites { throw 'runtime missing' }
$rejected = $false
try { Restart-BridgeTask } catch { if ($_.Exception.Message -ne 'runtime missing') { throw }; $rejected=$true }
if (-not $rejected) { throw 'Missing runtime did not fail' }
function Assert-BridgeLaunchPrerequisites { $script:PythonPath='C:\base\pythonw.exe' }
$rejected = $false
try { Restart-BridgeTask } catch { if ($_.Exception.Message -notlike '*Unrecognized bridge task launcher*') { throw }; $rejected=$true }
if (-not $rejected) { throw 'Unknown task action was accepted' }
''')

    def test_install_and_run_share_isolated_gui_bootstrap(self):
        self.run_script(r'''
$script:PythonPath='C:\base\pythonw.exe'
$script:BootstrapPath='H:\bridge\scripts\run-http-daemon.pyw'
function Assert-BridgeLaunchPrerequisites {}
function Ensure-UserToken { $false }
function Get-BridgeTask { $null }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) $script:installed=@($Execute,$Argument); [pscustomobject]@{} }
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User) [pscustomobject]@{} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) if($RunLevel -ne 'Limited'){throw 'Unexpected elevation'}; [pscustomobject]@{} }
function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,[switch]$Hidden,$MultipleInstances,$RestartCount,$RestartInterval,$ExecutionTimeLimit) [pscustomobject]@{} }
function New-ScheduledTask { param($Action,$Trigger,$Principal,$Settings) [pscustomobject]@{} }
function Register-ScheduledTask { param($TaskName,$TaskPath,$InputObject,[switch]$Force) }
function Start-BridgeTask {}
Install-BridgeTask
if ($script:installed[0] -ne $script:PythonPath -or $script:installed[1] -notlike '-I -S *run-http-daemon.pyw --http-port 8765*') { throw 'Install did not use isolated GUI bootstrap' }
function Start-Process { param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru,[switch]$Wait)
    if ($FilePath -ne $script:installed[0] -or $ArgumentList -cne $script:installed[1] -or $WindowStyle -ne 'Hidden') { throw 'Run differs from installed action' }
    [pscustomobject]@{ExitCode=0}
}
Invoke-BridgeDaemon
''')

    def test_stop_tracks_bootstrap_by_exact_repo_path_and_installed_port(self):
        self.run_script(r'''
$script:RepositoryRoot='H:\bridge'
$script:BootstrapPath='H:\bridge\scripts\run-http-daemon.pyw'
$script:terminated=@()
$cmd='"C:\base\pythonw.exe" -I -S H:\bridge\scripts\run-http-daemon.pyw --http-port 18765'
$script:fixtures=@(
    [pscustomobject]@{ProcessId=11;ParentProcessId=100;ExecutablePath='C:\base\pythonw.exe';CommandLine=$cmd;CreationDate=1},
    [pscustomobject]@{ProcessId=12;ParentProcessId=100;ExecutablePath='C:\base\pythonw.exe';CommandLine=($cmd -replace 'H:\\bridge\\','H:\other\');CreationDate=1},
    [pscustomobject]@{ProcessId=13;ParentProcessId=100;ExecutablePath='C:\base\pythonw.exe';CommandLine=($cmd -replace '18765','18766');CreationDate=1}
)
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction)
    if($Filter -match '^ProcessId = (\d+)$'){return $script:fixtures | Where-Object ProcessId -eq ([int]$Matches[1])}; $script:fixtures
}
function Get-BridgeTask { [pscustomobject]@{State='Ready';Actions=@([pscustomobject]@{Arguments=$cmd})} }
function Invoke-CimMethod { param($InputObject,$MethodName) $script:terminated += $InputObject.ProcessId; [pscustomobject]@{ReturnValue=0} }
Stop-BridgeTask
if(($script:terminated -join ',') -ne '11'){throw "Unsafe process cleanup: $script:terminated"}
''')

    def test_install_can_replace_legacy_wrapper_without_unsupported_migration(self):
        self.run_script(r'''
$script:events=@()
function Get-BridgeTask { [pscustomobject]@{State='Running';Actions=@([pscustomobject]@{Arguments='-File old-wrapper.ps1 Run'})} }
function Get-BridgeProcesses { param($Port) if($Port -ne 8765){throw 'Wrong replacement port'}; @() }
function Stop-ScheduledTask { param($TaskName,$TaskPath) $script:events += 'stop-exact-task' }
Stop-BridgeTask -ForReplacement
if(($script:events -join ',') -ne 'stop-exact-task'){throw 'Legacy wrapper was not stopped for reinstall'}
''')

    def test_stop_scopes_processes_and_rechecks_pid_identity(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not installed")
        source = SCRIPT_PATH.read_text(encoding="utf-8").split('switch ($Action)', 1)[0]
        source += r'''
$script:RepositoryRoot = 'H:\test-bridge'
$script:PythonPath = 'H:\test-bridge\.venv\Scripts\pythonw.exe'
$script:BootstrapPath = 'H:\test-bridge\scripts\run-http-daemon.pyw'
$script:terminated = @()
$command = 'pythonw.exe -m sketchup_mcp.http_daemon --http-port 8765'
$script:fixtures = @(
    [pscustomobject]@{ProcessId=1; ParentProcessId=100; ExecutablePath=$script:PythonPath; CommandLine=$command; CreationDate=1},
    [pscustomobject]@{ProcessId=2; ParentProcessId=1; ExecutablePath='C:\runtime\pythonw.exe'; CommandLine=$command; CreationDate=1},
    [pscustomobject]@{ProcessId=3; ParentProcessId=100; ExecutablePath='H:\other\.venv\Scripts\pythonw.exe'; CommandLine=$command; CreationDate=1},
    [pscustomobject]@{ProcessId=4; ParentProcessId=100; ExecutablePath=$script:PythonPath; CommandLine=($command -replace '8765','8766'); CreationDate=1}
)
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($Filter -match '^ProcessId = (\d+)$') {
        $found = $script:fixtures | Where-Object ProcessId -eq ([int]$Matches[1])
        if ($found.ProcessId -eq 2) {
            # 模拟 PID 被复用，禁止终止这个新进程。
            return [pscustomobject]@{ProcessId=2; ExecutablePath=$found.ExecutablePath; CommandLine=$found.CommandLine; CreationDate=2}
        }
        return $found
    }
    return $script:fixtures
}
function Get-BridgeTask { return [pscustomobject]@{State='Ready'; Actions=@([pscustomobject]@{Arguments='-m sketchup_mcp.http_daemon --http-port 8765'})} }
function Invoke-CimMethod {
    param($InputObject, $MethodName)
    $script:terminated += $InputObject.ProcessId
    return [pscustomobject]@{ReturnValue=0}
}
$selected = @(Get-BridgeProcessIds)
if (($selected -join ',') -ne '2,1') { throw "Wrong process selection: $selected" }
Stop-BridgeTask
if (($script:terminated -join ',') -ne '1') { throw "Unsafe termination: $script:terminated" }
'''
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "scoped-stop.ps1"
            probe.write_text(source, encoding="utf-8-sig")
            result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(probe)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_http_console_script_is_packaged(self):
        project = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn(
            'sketchup-mcp-http = "sketchup_mcp.http_server:main"',
            project,
        )
        self.assertIn('"mcp[cli]>=1.27.2,<1.28"', project)
        self.assertIn("mcp[cli]>=1.27.2,<1.28", requirements.splitlines())

    def test_management_script_covers_the_daemon_contract(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        for action in ("Install", "Start", "Stop", "Restart", "Status", "RotateToken", "Uninstall"):
            self.assertIn(f'"{action}"', script)

        for required_text in (
            '$script:TaskName = "SketchUpMCP HTTP Bridge"',
            ".venv\\Scripts\\pythonw.exe",
            '"Run"',
            'scripts\\run-http-daemon.pyw',
            '"--http-port"',
            '"--sketchup-port"',
            '"--allow-autostart"',
            '"--sketchup-executable"',
            '"--startup-timeout"',
            '"--request-timeout-ms"',
            '"--session-idle-timeout"',
            'New-ScheduledTaskTrigger -AtLogOn',
            '-Hidden',
            '-MultipleInstances IgnoreNew',
            '-RestartCount 3',
            '-ExecutionTimeLimit ([TimeSpan]::Zero)',
            'New-Object byte[] 32',
            'SKETCHUP_MCP_HTTP_TOKEN',
            'SendMessageTimeout',
            '/healthz',
        ):
            self.assertIn(required_text, script)

        self.assertNotIn("Stop-Process", script)
        self.assertNotIn("taskkill", script.lower())

    def test_task_runner_refreshes_token_without_exposing_it_in_task_arguments(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('-Execute $script:PythonPath', script)
        self.assertNotIn('powershell.exe', script.lower())
        self.assertNotIn('$script:PowerShellPath', script)
        task_arguments = script.split("function Get-BridgeTaskArguments", 1)[1].split(
            "function Assert-BridgeLaunchPrerequisites", 1
        )[0]
        self.assertNotIn("Get-UserToken", task_arguments)
        self.assertNotIn("$token", task_arguments)

    def test_wait_accepts_disabled_and_other_non_running_task_states(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn('return [string]$Task.State -in @("Running", "Queued")', script)
        wait_body = script.split("function Wait-BridgeTaskStopped", 1)[1].split(
            "function Restart-BridgeTask", 1
        )[0]
        self.assertIn("-not (Test-BridgeTaskActiveState -Task $task)", wait_body)
        self.assertNotIn('$task.State -eq "Ready"', wait_body)

    def test_management_script_keeps_defaults_and_token_cleanup_scoped(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("[int]$HttpPort = 8765", script)
        self.assertIn("[int]$SketchUpPort = 9876", script)
        self.assertIn("[switch]$AllowAutostart = $true", script)
        self.assertIn("SketchUp 2022\\SketchUp.exe", script)
        self.assertIn("[int]$StartupTimeout = 90", script)
        self.assertIn("[int]$RequestTimeoutMs = 15000", script)
        self.assertIn("[int]$SessionIdleTimeout = 1800", script)
        self.assertIn('%LOCALAPPDATA%\\SketchUpMCP\\logs\\http-daemon.log', script)
        self.assertIn("if ($RemoveToken)", script)
        self.assertIn("SetEnvironmentVariable($script:TokenEnvironmentVariable, $null, \"User\")", script)
        self.assertIn("Repository files and logs were preserved.", script)

    def test_management_script_parses_in_powershell(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not installed")

        escaped_path = str(SCRIPT_PATH).replace("'", "''")
        command = (
            "$tokens = $null; "
            "$errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped_path}', [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { [Console]::Error.WriteLine($_.ToString()) }; exit 1 "
            "}"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
