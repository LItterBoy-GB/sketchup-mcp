import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "manage-http-daemon.ps1"


class DaemonManagementTests(unittest.TestCase):
    def test_stop_scopes_processes_and_rechecks_pid_identity(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not installed")
        source = SCRIPT_PATH.read_text(encoding="utf-8").split('switch ($Action)', 1)[0]
        source += r'''
$script:RepositoryRoot = 'H:\test-bridge'
$script:PythonPath = 'H:\test-bridge\.venv\Scripts\pythonw.exe'
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
function Get-BridgeTask { return [pscustomobject]@{State='Ready'} }
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
            '.venv\\Scripts\\pythonw.exe',
            '"Run"',
            '"sketchup_mcp.http_daemon"',
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
