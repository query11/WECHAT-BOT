"""Windows process-lifetime regression. Uses temporary heartbeat workers, never the real bridge."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ctypes import wintypes
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                 'System32/WindowsPowerShell/v1.0/powershell.exe')


class StartupInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('reserved', wintypes.LPWSTR),
                ('desktop', wintypes.LPWSTR), ('title', wintypes.LPWSTR),
                ('x', wintypes.DWORD), ('y', wintypes.DWORD),
                ('xsize', wintypes.DWORD), ('ysize', wintypes.DWORD),
                ('xchars', wintypes.DWORD), ('ychars', wintypes.DWORD),
                ('fill', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('show', wintypes.WORD), ('reserved_size', wintypes.WORD),
                ('reserved_data', ctypes.c_void_p), ('input', wintypes.HANDLE),
                ('output', wintypes.HANDLE), ('error', wintypes.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', wintypes.HANDLE), ('thread', wintypes.HANDLE),
                ('pid', wintypes.DWORD), ('tid', wintypes.DWORD)]


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                ('maximum', ctypes.c_size_t), ('active_limit', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object integration test')
class ProcessIsolationTests(unittest.TestCase):
    def setUp(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        prototypes = {
            'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            'CreateProcessW': ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                wintypes.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'IsProcessInJob': ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            'ResumeThread': ([wintypes.HANDLE], wintypes.DWORD),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in prototypes.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        (ROOT / '.runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='process-isolation-', dir=ROOT / '.runtime')
        self.directory = Path(self.temp.name)
        self.handles, self.job = [], None
        self.addCleanup(self.cleanup_processes)
        (self.directory / 'worker.py').write_text(
            "from pathlib import Path\nimport sys,time\np=Path(sys.argv[1])\n"
            "while not (p.parent/'stop').exists():\n    p.write_text(str(time.time()))\n    time.sleep(0.1)\n",
            encoding='utf-8')
        (self.directory / 'ui.ps1').write_text('''param([string]$Heartbeat)
Add-Type -AssemblyName System.Windows.Forms
$context=New-Object Windows.Forms.ApplicationContext
$expires=[DateTime]::UtcNow.AddSeconds(30)
$timer=New-Object Windows.Forms.Timer;$timer.Interval=100
$timer.Add_Tick({
 if((Test-Path -LiteralPath (Join-Path (Split-Path $Heartbeat) 'stop')) -or [DateTime]::UtcNow -gt $expires){$context.ExitThread();return}
 [IO.File]::WriteAllText($Heartbeat,[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString())
})
$timer.Start();[Windows.Forms.Application]::Run($context);$timer.Dispose();$context.Dispose()
''', encoding='utf-8-sig')
        (self.directory / 'owner.ps1').write_text('''param([string]$Fixture,[string]$Source,[string]$Python,[switch]$Denied)
$ErrorActionPreference='Stop'
. (Join-Path $Source 'process_launcher.ps1')
$worker=Join-Path $Fixture 'worker.py'
$backendArgs='"'+$worker+'" "'+(Join-Path $Fixture 'backend-beat')+'"'
if($Denied){
 try{$unexpected=Start-WeChatIndependentProcess $Python $backendArgs $Fixture;$result=@{started=$true;pid=$unexpected.Id}}
 catch{$result=@{started=$false;error=$_.Exception.Message}}
 [IO.File]::WriteAllText((Join-Path $Fixture 'ready.json'),($result|ConvertTo-Json))
}else{
 try{
 $legacyArgs='"'+$worker+'" "'+(Join-Path $Fixture 'legacy-beat')+'"'
 $legacy=Start-Process -FilePath $Python -WindowStyle Hidden -WorkingDirectory $Fixture -ArgumentList $legacyArgs -PassThru
 $backend=Start-WeChatIndependentProcess $Python $backendArgs $Fixture
 $uiArgs='-NoProfile -STA -ExecutionPolicy Bypass -File "'+(Join-Path $Fixture 'ui.ps1')+'" -Heartbeat "'+(Join-Path $Fixture 'ui-beat')+'"'
 $ui=Start-WeChatIndependentProcess (Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe') $uiArgs $Fixture
 $result=@{owner=$PID;legacy=$legacy.Id;backend=$backend.Id;ui=$ui.Id}
 [IO.File]::WriteAllText((Join-Path $Fixture 'ready.json'),($result|ConvertTo-Json))
 }catch{
  $result=@{error=$_.Exception.Message}
  if($legacy){$result.legacy=$legacy.Id};if($backend){$result.backend=$backend.Id};if($ui){$result.ui=$ui.Id}
  [IO.File]::WriteAllText((Join-Path $Fixture 'ready.json'),($result|ConvertTo-Json))
 }
}
while($true){Start-Sleep -Milliseconds 100}
''', encoding='utf-8-sig')

    def check(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def wait_for(self, predicate, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.fail('Timed out waiting for the isolated process fixture')

    def start_owner(self, allow_breakaway, apartment='-STA'):
        self.job = self.check(self.kernel.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | (0x0800 if allow_breakaway else 0)
        self.check(self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        args = [POWERSHELL, '-NoProfile', apartment, '-ExecutionPolicy', 'Bypass',
                '-File', str(self.directory / 'owner.ps1'), '-Fixture', str(self.directory),
                '-Source', str(ROOT / 'desktop'), '-Python', sys.executable]
        if not allow_breakaway:
            args.append('-Denied')
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        startup, info = StartupInfo(), ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        self.check(self.kernel.CreateProcessW(POWERSHELL, command, None, None, False,
            0x01000000 | 0x08000000 | 0x00000004, None, str(self.directory),
            ctypes.byref(startup), ctypes.byref(info)))
        self.handles.append(info.process)
        try:
            self.check(self.kernel.AssignProcessToJobObject(self.job, info.process))
            self.assertNotEqual(self.kernel.ResumeThread(info.thread), 0xffffffff)
        finally:
            self.kernel.CloseHandle(info.thread)
        data = self.wait_for(lambda: json.loads((self.directory / 'ready.json').read_text(encoding='utf-8-sig')))
        if allow_breakaway and 'error' in data:
            for name in ('legacy', 'backend', 'ui'):
                if data.get(name):
                    self.open_owned_process(data[name])
            self.fail(data['error'])
        return info, data

    def open_owned_process(self, pid):
        handle = self.check(self.kernel.OpenProcess(0x0400 | 0x100000 | 0x0001, False, pid))
        self.handles.append(handle)
        return handle

    def in_job(self, handle, job=None):
        result = wintypes.BOOL()
        self.check(self.kernel.IsProcessInJob(handle, job, ctypes.byref(result)))
        return bool(result.value)

    def close_owner_job(self):
        self.check(self.kernel.CloseHandle(self.job))
        self.job = None

    def cleanup_processes(self):
        self.directory.resolve().relative_to((ROOT / '.runtime').resolve())
        (self.directory / 'stop').touch()
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 1000) == 0x102:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 2000)
            self.kernel.CloseHandle(handle)
        self.temp.cleanup()

    def test_backend_and_gui_loop_survive_host_job_shutdown(self):
        owner, data = self.start_owner(True)
        legacy = self.open_owned_process(data['legacy'])
        backend = self.open_owned_process(data['backend'])
        ui = self.open_owned_process(data['ui'])
        self.assertTrue(self.in_job(legacy, self.job))
        self.assertFalse(self.in_job(backend, self.job))
        self.assertFalse(self.in_job(ui, self.job))
        self.wait_for(lambda: all((self.directory / name).exists()
            for name in ('legacy-beat', 'backend-beat', 'ui-beat')))
        backend_before, ui_before = self.wait_for(lambda: (
            float((self.directory / 'backend-beat').read_text()),
            int((self.directory / 'ui-beat').read_text())))
        self.close_owner_job()
        self.assertEqual(self.kernel.WaitForSingleObject(owner.process, 3000), 0)
        self.assertEqual(self.kernel.WaitForSingleObject(legacy, 3000), 0)
        self.wait_for(lambda: float((self.directory / 'backend-beat').read_text()) > backend_before
            and int((self.directory / 'ui-beat').read_text()) > ui_before)
        self.assertEqual(self.kernel.WaitForSingleObject(backend, 0), 0x102)
        self.assertEqual(self.kernel.WaitForSingleObject(ui, 0), 0x102)

    def test_explorer_broker_works_when_the_host_denies_breakaway(self):
        _, data = self.start_owner(False)
        self.assertTrue(data['started'], data.get('error', ''))
        backend = self.open_owned_process(data['pid'])
        self.assertFalse(self.in_job(backend, self.job))
        before = self.wait_for(lambda: float((self.directory / 'backend-beat').read_text()))
        self.close_owner_job()
        self.wait_for(lambda: float((self.directory / 'backend-beat').read_text()) > before)
        self.assertEqual(self.kernel.WaitForSingleObject(backend, 0), 0x102)

    def test_mta_command_host_can_use_the_explorer_broker(self):
        _, data = self.start_owner(False, '-MTA')
        self.assertTrue(data['started'], data.get('error', ''))
        backend = self.open_owned_process(data['pid'])
        self.assertFalse(self.in_job(backend, self.job))
        before = self.wait_for(lambda: float((self.directory / 'backend-beat').read_text()))
        self.close_owner_job()
        self.wait_for(lambda: float((self.directory / 'backend-beat').read_text()) > before)

    def test_bootstrap_refuses_a_kill_on_close_host_job(self):
        launch = ROOT / '.runtime' / 'process-launch'
        launch.mkdir(parents=True, exist_ok=True)
        nonce = uuid.uuid4().hex
        request = launch / ('request-' + nonce + '.json')
        result = launch / ('result-' + nonce + '.json')
        for path in (request, result, result.with_suffix('.json.tmp')):
            self.addCleanup(path.unlink, missing_ok=True)
        request.write_text(json.dumps({
            'nonce': nonce, 'caller_pid': os.getpid(),
            'created_ticks': int(time.time() * 10 ** 7) + EPOCH_TICKS,
            'executable': sys.executable, 'arguments': '--version',
            'directory': str(self.directory), 'encoding': '', 'debug_log': False,
        }), encoding='utf-8')
        (self.directory / 'request-path.txt').write_text(str(request), encoding='utf-8')
        (self.directory / 'owner.ps1').write_text('''param([string]$Fixture,[string]$Source,[string]$Python,[switch]$Denied)
$ErrorActionPreference='Stop'
$request=[IO.File]::ReadAllText((Join-Path $Fixture 'request-path.txt'))
& (Join-Path $Source 'process_bootstrap.ps1') -Request $request
$result=Join-Path (Split-Path $request) ((Split-Path $request -Leaf).Replace('request-','result-'))
Copy-Item -LiteralPath $result -Destination (Join-Path $Fixture 'ready.json')
while($true){Start-Sleep -Milliseconds 100}
''', encoding='ascii')
        _, data = self.start_owner(False)
        self.assertFalse(data['ok'], data)
        self.assertFalse(data['independent'], data)
        self.assertIn('kill-on-close host job', data['error'])
        self.assertNotIn('pid', data)


EPOCH_TICKS = 621355968000000000   # 1970-01-01 的 .NET ticks，Python 时间要转成它


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration test')
class BootstrapRequestTests(unittest.TestCase):
    """process_bootstrap.ps1 的入口守卫：伪造、过期、错名的请求必须被就地拒掉。

    这一组不需要 GUI、不需要 Explorer、也不需要 Job Object——被拒的请求在
    Add-Type 之前就抛了，所以在没有桌面会话的机器上一样能跑。真正的启动成功
    路径要 Explorer 代理，那部分见 ProcessIsolationTests。
    """

    def setUp(self):
        (ROOT / '.runtime').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='bootstrap-', dir=ROOT / '.runtime')
        self.directory = Path(self.temp.name)
        self.launch = ROOT / '.runtime' / 'process-launch'
        self.launch.mkdir(parents=True, exist_ok=True)
        self.temporary_paths = []
        self.addCleanup(self.cleanup_paths)
        self.script = ROOT / 'desktop' / 'process_bootstrap.ps1'

    def cleanup_paths(self):
        for path in self.temporary_paths:
            path.unlink(missing_ok=True)
        self.temp.cleanup()

    def track(self, *paths):
        self.temporary_paths.extend(paths)
        return paths[0]

    def ticks(self, offset_seconds=0):
        return (int(time.time() * 10 ** 7) + EPOCH_TICKS
                + int(offset_seconds * 10 ** 7))

    def payload(self, nonce, offset_seconds=0, arguments='--fake-secret-value'):
        return {'nonce': nonce, 'caller_pid': os.getpid(),
                'created_ticks': self.ticks(offset_seconds),
                'executable': sys.executable, 'arguments': arguments,
                'directory': str(self.directory), 'encoding': '', 'debug_log': False}

    def request_in_launch(self, payload_nonce=None, offset_seconds=0,
                          arguments='--fake-secret-value'):
        """在真正的 process-launch 里放一份请求，返回 (请求路径, 结果路径)。"""
        nonce = uuid.uuid4().hex
        request = self.launch / ('request-%s.json' % nonce)
        result = self.launch / ('result-%s.json' % nonce)
        request.write_text(json.dumps(self.payload(payload_nonce or nonce,
                                                   offset_seconds, arguments)),
                           encoding='utf-8')
        return self.track(request, result, self.launch / (result.name + '.tmp')), result

    def run_bootstrap(self, request):
        return subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                               '-File', str(self.script), '-Request', str(request)],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=60)

    def test_setup_creates_runtime_in_a_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(__name__ + '.ROOT', Path(directory)):
                fixture = BootstrapRequestTests()
                try:
                    fixture.setUp()
                    self.assertTrue((Path(directory) / '.runtime' / 'process-launch').is_dir())
                finally:
                    fixture.doCleanups()

    def test_request_outside_the_launch_directory_is_refused(self):
        request = self.track(self.directory / ('request-%s.json' % uuid.uuid4().hex))
        request.write_text(json.dumps(self.payload(uuid.uuid4().hex)), encoding='utf-8')
        completed = self.run_bootstrap(request)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('Invalid process launch request path',
                      completed.stdout + completed.stderr)
        self.assertEqual(list(self.directory.glob('result-*.json')), [])

    def test_misnamed_request_inside_the_launch_directory_is_refused(self):
        request = self.track(self.launch / 'request-not-a-nonce.json')
        request.write_text(json.dumps(self.payload('not-a-nonce')), encoding='utf-8')
        completed = self.run_bootstrap(request)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn('Invalid process launch request path',
                      completed.stdout + completed.stderr)
        self.assertFalse((self.launch / 'result-not-a-nonce.json').exists())
        self.assertFalse((self.launch / 'result-not-a-nonce.json.tmp').exists())

    def test_mismatched_nonce_is_refused_and_reported_under_the_filename(self):
        request, result_path = self.request_in_launch(payload_nonce=uuid.uuid4().hex)
        completed = self.run_bootstrap(request)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(result_path.read_text(encoding='utf-8-sig'))
        self.assertFalse(result['ok'], result)
        self.assertFalse(result['independent'], result)
        self.assertIn('Expired or mismatched', result['error'])
        self.assertEqual(result['nonce'], request.name[8:-5])
        self.assertNotIn('pid', result)

    def test_expired_and_future_requests_are_refused(self):
        for offset in (-60, 60):
            request, result_path = self.request_in_launch(offset_seconds=offset)
            completed = self.run_bootstrap(request)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(result_path.read_text(encoding='utf-8-sig'))
            self.assertFalse(result['ok'], result)
            self.assertIn('Expired or mismatched', result['error'])

    def test_rejected_result_carries_no_arguments_and_no_extra_keys(self):
        secret = 'super-secret-argument-value'
        request, result_path = self.request_in_launch(payload_nonce=uuid.uuid4().hex,
                                                      arguments=secret)
        self.run_bootstrap(request)
        text = result_path.read_text(encoding='utf-8-sig')
        self.assertNotIn(secret, text)
        self.assertLessEqual(set(json.loads(text)), {
            'nonce', 'ok', 'independent', 'error', 'bootstrap_pid', 'parent_pid',
            'job_flags', 'caller_in_bootstrap_job', 'program_path', 'program_exists',
            'pid'})


if __name__ == '__main__':
    unittest.main()
