using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

// Persistent desktop processes must not inherit a command runner's kill-on-close job.
public static class WeChatIndependentProcess {
    const uint CreateSuspended = 0x00000004;
    const uint CreateBreakawayFromJob = 0x01000000;
    const uint CreateNoWindow = 0x08000000;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct StartupInfo {
        public uint cb;
        public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xChars, yChars, fill, flags;
        public ushort showWindow, reservedSize;
        public IntPtr reservedData, input, output, error;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInformation {
        public IntPtr process, thread;
        public uint processId, threadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcessW(string application, StringBuilder commandLine,
        IntPtr processAttributes, IntPtr threadAttributes, bool inheritHandles,
        uint flags, IntPtr environment, string directory,
        ref StartupInfo startup, out ProcessInformation info);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool inJob);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, IntPtr info, uint size, out uint returned);

    public static uint CurrentJobFlags() {
        var buffer = Marshal.AllocHGlobal(144);
        try {
            uint returned;
            if (!QueryInformationJobObject(IntPtr.Zero, 9, buffer, 144, out returned)) {
                int error = Marshal.GetLastWin32Error();
                if (IsCurrentProcessInJob())
                    throw new Win32Exception(error, "Cannot inspect the host job lifetime policy.");
                return 0;
            }
            return (uint)Marshal.ReadInt32(buffer, 16);
        } finally { Marshal.FreeHGlobal(buffer); }
    }

    public static bool CurrentJobContains(int pid) {
        var buffer = Marshal.AllocHGlobal(65536);
        try {
            uint returned;
            if (!QueryInformationJobObject(IntPtr.Zero, 3, buffer, 65536, out returned)) return false;
            int count = Math.Min(Marshal.ReadInt32(buffer, 4), 1000);
            for (int i = 0; i < count; i++)
                if (Marshal.ReadIntPtr(buffer, 8 + i * IntPtr.Size).ToInt64() == pid) return true;
            return false;
        } finally { Marshal.FreeHGlobal(buffer); }
    }

    public static bool IsCurrentProcessInJob() {
        bool inJob;
        if (!IsProcessInJob(GetCurrentProcess(), IntPtr.Zero, out inJob))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect the launcher process job.");
        return inJob;
    }

    public static Process Start(string executable, string arguments, string directory) {
        if (!Path.IsPathRooted(executable) || !File.Exists(executable))
            throw new ArgumentException("An existing absolute executable path is required.", "executable");
        directory = Path.GetFullPath(directory);
        if (!Directory.Exists(directory)) throw new DirectoryNotFoundException(directory);

        var startup = new StartupInfo();
        startup.cb = (uint)Marshal.SizeOf(typeof(StartupInfo));
        uint flags = CreateNoWindow | CreateSuspended;
        if (IsCurrentProcessInJob()) {
            if ((CurrentJobFlags() & 0x2000) != 0)
                throw new InvalidOperationException("A kill-on-close host must use the Explorer launch broker.");
            flags |= CreateBreakawayFromJob;
        }
        var command = new StringBuilder("\"" + executable + "\" " + (arguments ?? ""));
        ProcessInformation info;
        if (!CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, false,
                flags, IntPtr.Zero, directory, ref startup, out info))
            throw new Win32Exception(Marshal.GetLastWin32Error(),
                "Cannot start an independent process. Open the assistant from Windows Explorer if the host denies job breakaway.");

        Process result = null;
        try {
            // Windows may retain a shared system job. The creator must already be
            // outside the command runner's kill-on-close jobs before this point.
            bool inJob;
            if (!IsProcessInJob(info.process, IntPtr.Zero, out inJob))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot inspect the started process job.");
            result = Process.GetProcessById((int)info.processId);
            if (ResumeThread(info.thread) == uint.MaxValue)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot resume the independent process.");
            return result;
        } catch {
            TerminateProcess(info.process, 1);
            if (result != null) result.Dispose();
            throw;
        } finally {
            CloseHandle(info.thread);
            CloseHandle(info.process);
        }
    }
}
