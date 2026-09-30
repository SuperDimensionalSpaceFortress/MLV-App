# um-display-mapping-probe-job.ps1 (UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1) -- RUNS ON ULTRA-MAGNUS.
#
# Read-only. Collects, in ONE process and ONE moment, every view of "which monitor is which" the
# display-identity chain touches, so the Qt view can be compared against the Windows views:
#   1. process context      : user, session id (an interactive console is session 1)
#   2. EnumDisplayDevices   : adapter DeviceName (\\.\DISPLAYn) + monitor DeviceString/DeviceID, and the
#                             current mode + desktop position (what Get-AttrCudaWindowsDisplayInventory reads)
#   3. GetMonitorInfo       : monitor rect + szDevice per HMONITOR (EnumDisplayMonitors)
#   4. QueryDisplayConfig   : per active path, source GDI name (viewGdiDeviceName) paired with the target's
#                             EDID manufacturer id / product code / friendly name / device path
#   5. WmiMonitorID (WMI)   : decoded manufacturer/product/serial/user-friendly name per monitor instance
#   6. the Qt probe         : qscreen-probe.exe (same Qt 6.10.2 windows QPA as the app), unpacked from the
#                             side-file zip and run with a hard timeout; raw stdout is kept verbatim
# Nothing here mutates display state (no ChangeDisplaySettings/SPI_SET*), kills a process, or touches a clip.
# ASCII only (PS5.1-safe traps in project memory); the output is one BEGIN/END delimited block per section.
param(
    [Parameter(Mandatory)][string]$ProbeZipName,
    [Parameter(Mandatory)][string]$ProbeZipSha256,
    [int]$ProbeTimeoutSec = 60
)
$ErrorActionPreference = 'Stop'

function Write-Section {
    param([string]$Name, [scriptblock]$Body)
    Write-Output "=====BEGIN $Name====="
    try { & $Body } catch { Write-Output ("SECTION_ERROR " + $_.Exception.GetType().Name + ": " + $_.Exception.Message) }
    Write-Output "=====END $Name====="
}

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace UmMap
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DispDev
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DevMode
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion; public short dmDriverVersion; public short dmSize; public short dmDriverExtra;
        public int dmFields; public int dmPositionX; public int dmPositionY; public int dmDisplayOrientation;
        public int dmDisplayFixedOutput; public short dmColor; public short dmDuplex; public short dmYResolution;
        public short dmTTOption; public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels; public int dmBitsPerPel; public int dmPelsWidth; public int dmPelsHeight;
        public int dmDisplayFlags; public int dmDisplayFrequency; public int dmICMMethod; public int dmICMIntent;
        public int dmMediaType; public int dmDitherType; public int dmReserved1; public int dmReserved2;
        public int dmPanningWidth; public int dmPanningHeight;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct Rect { public int left; public int top; public int right; public int bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct MonitorInfoEx
    {
        public int cbSize; public Rect rcMonitor; public Rect rcWork; public int dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
    }

    public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, IntPtr lprc, IntPtr data);

    public class MonRec { public string Device; public int L, T, R, B; public bool Primary; }
    public class CfgRec
    {
        public string GdiName; public string Friendly; public string DevicePath;
        public int EdidManufacturerId; public int EdidProductCodeId; public uint ConnectorInstance; public uint OutputTech;
    }

    public static class Native
    {
        [DllImport("user32.dll", EntryPoint = "EnumDisplayDevicesW", SetLastError = true)]
        public static extern bool EnumDisplayDevicesNull(IntPtr lpDevice, uint iDevNum, ref DispDev d, uint flags);
        [DllImport("user32.dll", EntryPoint = "EnumDisplayDevicesW", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplayDevicesNamed(string lpDevice, uint iDevNum, ref DispDev d, uint flags);
        [DllImport("user32.dll", EntryPoint = "EnumDisplaySettingsW", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplaySettings(string dev, int mode, ref DevMode dm);
        [DllImport("user32.dll")]
        public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc proc, IntPtr data);
        [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW", CharSet = CharSet.Unicode)]
        public static extern bool GetMonitorInfo(IntPtr hMon, ref MonitorInfoEx mi);
        [DllImport("user32.dll")]
        public static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll")]
        public static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);
        [DllImport("user32.dll")]
        public static extern int QueryDisplayConfig(uint flags, ref uint numPaths, byte[] paths, ref uint numModes, byte[] modes, IntPtr topology);
        [DllImport("user32.dll")]
        public static extern int DisplayConfigGetDeviceInfo(byte[] packet);

        public static List<MonRec> Monitors()
        {
            var list = new List<MonRec>();
            MonitorEnumProc cb = delegate (IntPtr h, IntPtr hdc, IntPtr r, IntPtr d)
            {
                var mi = new MonitorInfoEx();
                mi.cbSize = Marshal.SizeOf(typeof(MonitorInfoEx));
                if (GetMonitorInfo(h, ref mi))
                {
                    var m = new MonRec();
                    m.Device = mi.szDevice; m.L = mi.rcMonitor.left; m.T = mi.rcMonitor.top;
                    m.R = mi.rcMonitor.right; m.B = mi.rcMonitor.bottom; m.Primary = (mi.dwFlags & 1) != 0;
                    list.Add(m);
                }
                return true;
            };
            EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, cb, IntPtr.Zero);
            GC.KeepAlive(cb);
            return list;
        }

        static string Wide(byte[] b, int offset, int chars)
        {
            string s = Encoding.Unicode.GetString(b, offset, chars * 2);
            int z = s.IndexOf('\0');
            return z >= 0 ? s.Substring(0, z) : s;
        }

        static void PutHeader(byte[] p, int type, int size, byte[] adapterLuid, uint id)
        {
            BitConverter.GetBytes(type).CopyTo(p, 0);
            BitConverter.GetBytes(size).CopyTo(p, 4);
            Array.Copy(adapterLuid, 0, p, 8, 8);
            BitConverter.GetBytes(id).CopyTo(p, 16);
        }

        // DISPLAYCONFIG_PATH_INFO is 72 bytes: sourceInfo{adapterId(8) id(4) modeInfoIdx(4) statusFlags(4)}=20,
        // targetInfo{adapterId(8) id(4) modeInfoIdx(4) outputTech(4) rotation(4) scaling(4) refresh(8)
        // scanLine(4) targetAvailable(4) statusFlags(4)}=48, flags(4).
        public static List<CfgRec> ActivePaths(out string error)
        {
            error = null;
            var list = new List<CfgRec>();
            uint np, nm;
            int rc = GetDisplayConfigBufferSizes(2u, out np, out nm);   // QDC_ONLY_ACTIVE_PATHS
            if (rc != 0) { error = "GetDisplayConfigBufferSizes=" + rc; return list; }
            byte[] paths = new byte[np * 72]; byte[] modes = new byte[nm * 64];
            rc = QueryDisplayConfig(2u, ref np, paths, ref nm, modes, IntPtr.Zero);
            if (rc != 0) { error = "QueryDisplayConfig=" + rc; return list; }
            for (int i = 0; i < np; i++)
            {
                int o = i * 72;
                byte[] srcLuid = new byte[8]; Array.Copy(paths, o, srcLuid, 0, 8);
                uint srcId = BitConverter.ToUInt32(paths, o + 8);
                byte[] tgtLuid = new byte[8]; Array.Copy(paths, o + 20, tgtLuid, 0, 8);
                uint tgtId = BitConverter.ToUInt32(paths, o + 28);

                var rec = new CfgRec();
                byte[] src = new byte[84];                    // SOURCE_DEVICE_NAME: header(20) + viewGdiDeviceName[32] wchar
                PutHeader(src, 1, 84, srcLuid, srcId);
                if (DisplayConfigGetDeviceInfo(src) == 0) rec.GdiName = Wide(src, 20, 32);

                byte[] tgt = new byte[420];                   // TARGET_DEVICE_NAME: header20 flags@20 tech@24 mfr@28 prod@30 conn@32 friendly[64]w@36 path[128]w@164
                PutHeader(tgt, 2, 420, tgtLuid, tgtId);
                if (DisplayConfigGetDeviceInfo(tgt) == 0)
                {
                    rec.OutputTech = BitConverter.ToUInt32(tgt, 24);
                    rec.EdidManufacturerId = BitConverter.ToUInt16(tgt, 28);
                    rec.EdidProductCodeId = BitConverter.ToUInt16(tgt, 30);
                    rec.ConnectorInstance = BitConverter.ToUInt32(tgt, 32);
                    rec.Friendly = Wide(tgt, 36, 64);
                    rec.DevicePath = Wide(tgt, 164, 128);
                }
                list.Add(rec);
            }
            return list;
        }
    }
}
'@

Write-Section 'CONTEXT' {
    $p = Get-Process -Id $PID
    Write-Output ("computer=" + $env:COMPUTERNAME)
    Write-Output ("user=" + [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
    Write-Output ("session_id=" + $p.SessionId)
    Write-Output ("pwsh=" + $PSVersionTable.PSVersion.ToString())
    Write-Output ("utc=" + [DateTime]::UtcNow.ToString('o'))
    Write-Output ("os=" + [Environment]::OSVersion.VersionString)
    Write-Output ("sm_cmonitors=" + [UmMap.Native]::GetSystemMetrics(80))
    try {
        $vm = @(Get-CimInstance Win32_ComputerSystem)
        Write-Output ("manufacturer=" + $vm[0].Manufacturer + " model=" + $vm[0].Model)
        $vmms = Get-Process vmwp -ErrorAction SilentlyContinue
        Write-Output ("hyperv_vm_worker_processes=" + @($vmms).Count)
        $cpu = (Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 3).CounterSamples | ForEach-Object { $_.CookedValue }
        Write-Output ("cpu_total_pct_samples=" + (($cpu | ForEach-Object { [math]::Round($_, 1) }) -join ','))
    } catch { Write-Output ("context_extra_error=" + $_.Exception.Message) }
}

Write-Section 'ENUM_DISPLAY_DEVICES' {
    $rows = @()
    $ai = 0
    while ($true) {
        $a = New-Object UmMap.DispDev; $a.cb = [Runtime.InteropServices.Marshal]::SizeOf($a)
        if (-not [UmMap.Native]::EnumDisplayDevicesNull([IntPtr]::Zero, $ai, [ref]$a, 0)) { break }
        $ai++
        $mi = 0
        $monRows = @()
        while ($true) {
            $m = New-Object UmMap.DispDev; $m.cb = [Runtime.InteropServices.Marshal]::SizeOf($m)
            if (-not [UmMap.Native]::EnumDisplayDevicesNamed($a.DeviceName, $mi, [ref]$m, 0)) { break }
            $mi++
            $mIf = New-Object UmMap.DispDev; $mIf.cb = [Runtime.InteropServices.Marshal]::SizeOf($mIf)
            $ifPath = $null
            if ([UmMap.Native]::EnumDisplayDevicesNamed($a.DeviceName, ($mi - 1), [ref]$mIf, 1)) { $ifPath = $mIf.DeviceID }   # EDD_GET_DEVICE_INTERFACE_NAME
            $monRows += [ordered]@{ deviceName = $m.DeviceName; deviceString = $m.DeviceString; stateFlags = ('0x{0:X}' -f $m.StateFlags); deviceId = $m.DeviceID; interfacePath = $ifPath }
        }
        $dm = New-Object UmMap.DevMode; $dm.dmSize = [Runtime.InteropServices.Marshal]::SizeOf($dm)
        $ok = [UmMap.Native]::EnumDisplaySettings($a.DeviceName, -1, [ref]$dm)
        $rows += [ordered]@{
            deviceName = $a.DeviceName; adapterString = $a.DeviceString; stateFlags = ('0x{0:X}' -f $a.StateFlags)
            attached = (($a.StateFlags -band 1) -ne 0); primary = (($a.StateFlags -band 4) -ne 0)
            modeCollected = $ok
            posX = $(if ($ok) { $dm.dmPositionX } else { $null }); posY = $(if ($ok) { $dm.dmPositionY } else { $null })
            width = $(if ($ok) { $dm.dmPelsWidth } else { $null }); height = $(if ($ok) { $dm.dmPelsHeight } else { $null })
            refreshHz = $(if ($ok) { $dm.dmDisplayFrequency } else { $null })
            monitors = $monRows
        }
    }
    Write-Output (ConvertTo-Json -InputObject @($rows) -Depth 6 -Compress)
}

Write-Section 'GET_MONITOR_INFO' {
    $mons = @([UmMap.Native]::Monitors() | ForEach-Object { [ordered]@{ szDevice = $_.Device; left = $_.L; top = $_.T; right = $_.R; bottom = $_.B; primary = $_.Primary } })
    Write-Output (ConvertTo-Json -InputObject $mons -Depth 4 -Compress)
}

Write-Section 'QUERY_DISPLAY_CONFIG' {
    $err = $null
    $paths = [UmMap.Native]::ActivePaths([ref]$err)
    if ($err) { Write-Output ("error=" + $err) }
    $rows = @($paths | ForEach-Object {
        $m = [int]$_.EdidManufacturerId
        # EDID manufacturer id is three 5-bit letters, big-endian in the raw EDID; the API returns it byte-swapped-or-not, so both decodes are reported
        $dec = { param($v) [string]::new(@([char](64 + (($v -shr 10) -band 31)), [char](64 + (($v -shr 5) -band 31)), [char](64 + ($v -band 31)))) }
        $swapped = (($m -band 0xFF) -shl 8) -bor (($m -shr 8) -band 0xFF)
        [ordered]@{
            gdiName = $_.GdiName; friendlyName = $_.Friendly; devicePath = $_.DevicePath
            edidManufacturerIdRaw = $m; edidManufacturerDecode = (& $dec $m); edidManufacturerDecodeSwapped = (& $dec $swapped)
            edidProductCodeId = [int]$_.EdidProductCodeId; edidProductCodeHex = ('{0:X4}' -f [int]$_.EdidProductCodeId)
            connectorInstance = $_.ConnectorInstance; outputTechnology = $_.OutputTech
        }
    })
    Write-Output (ConvertTo-Json -InputObject $rows -Depth 4 -Compress)
}

Write-Section 'WMI_MONITOR_ID' {
    $dec = { param($a) if ($null -eq $a) { '' } else { (-join ($a | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ })) } }
    $rows = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID | ForEach-Object {
        [ordered]@{
            instanceName = $_.InstanceName; active = $_.Active
            manufacturerName = (& $dec $_.ManufacturerName); productCodeId = (& $dec $_.ProductCodeID)
            serialNumberId = (& $dec $_.SerialNumberID); userFriendlyName = (& $dec $_.UserFriendlyName)
            yearOfManufacture = $_.YearOfManufacture; weekOfManufacture = $_.WeekOfManufacture
        }
    })
    Write-Output (ConvertTo-Json -InputObject $rows -Depth 4 -Compress)
}

Write-Section 'QT_PROBE' {
    $zip = Join-Path $PSScriptRoot $ProbeZipName
    if (-not (Test-Path -LiteralPath $zip)) { $zip = Join-Path 'G:\Temp\mlv-gpu-profile\agent\inbox' $ProbeZipName }
    if (-not (Test-Path -LiteralPath $zip)) { throw "probe zip not found: $ProbeZipName (PSScriptRoot=$PSScriptRoot)" }
    $sha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Output ("probe_zip_sha256=" + $sha)
    if ($sha -ne $ProbeZipSha256.ToLowerInvariant()) { throw "probe zip hash mismatch: expected $ProbeZipSha256" }
    $dir = Join-Path $env:TEMP ("qscreen-probe-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
    $exe = Join-Path $dir 'qscreen-probe.exe'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe; $psi.WorkingDirectory = $dir; $psi.UseShellExecute = $false
    # The preference values the probe evaluates with the app's own rule: the device the job-resolver
    # hands over for the ASUS, the ASUS's Qt/EDID name, the other device, a device that does not exist,
    # and a prefix-only device string (DISPLAY1 must never select DISPLAY10-style near matches).
    foreach ($p in @('\\.\DISPLAY1', 'PA329C', '\\.\DISPLAY2', '\\.\DISPLAY9', '\\.\DISPLAY')) {
        $psi.ArgumentList.Add('--display-prefer'); $psi.ArgumentList.Add($p)
    }
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outTask = $proc.StandardOutput.ReadToEndAsync(); $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($ProbeTimeoutSec * 1000)) {
        # only the process THIS job started, by its own handle
        $proc.Kill(); Write-Output "probe_timeout=1"
    }
    Write-Output ("probe_exit=" + $proc.ExitCode)
    Write-Output ($outTask.Result.TrimEnd())
    $e = $errTask.Result.TrimEnd()
    if ($e) { Write-Output ("probe_stderr=" + $e) }
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output "JOB_DONE"
