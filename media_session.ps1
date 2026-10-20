# WinMon media-session helper.
# WinMon media helper v2 (atomic snapshot writes).
#
# Reads the Windows "media session" (GSMTC) so WinMon can show what is playing
# and send play/pause/next/previous to one specific player.
#
# Why this file exists at all (and why it is PowerShell, not ctypes):
#   The WinRT class `GlobalSystemMediaTransportControlsSessionManager` cannot be
#   created from a plain C ABI:
#     * `RoActivateInstance`           -> E_NOTIMPL for this class
#     * `RoGetActivationFactory`       -> works, but the static entry point
#                                         `RequestAsync()` lives on the *statics*
#                                         interface, whose IID is only inside
#                                         Windows.Media.winmd (binary GuidAttribute).
#   Windows' own WinRT projection already knows that IID, and Windows ships it in
#   System.Runtime.WindowsRuntime.dll. Using it costs us nothing (still zero
#   third-party dependencies) and avoids re-implementing an ECMA-335 reader.
#
# Notes for anyone editing this file:
#   * Keep it ASCII-only, or save it with a UTF-8 BOM. Windows PowerShell 5.1
#     reads .ps1 files as ANSI unless there is a BOM, and non-ASCII text then
#     breaks the parser (this bit us when the file had Chinese comments).
#   * `IAsyncOperation<T>` has no GetAwaiter() in PowerShell: it surfaces as a
#     bare __ComObject, so we must convert it with AsTask<T> from
#     System.WindowsRuntimeSystemExtensions (loaded by path, by reflection).
#   * Output is one line of JSON with "ok" plus either "sessions" or "error".
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File media_session.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File media_session.ps1 -Command next -App MSEdge
#
# Snapshot mode (added 2026-10-12) -- this is what makes WinMon's media line fast:
#   powershell ... -File media_session.ps1 -Snapshot C:\path\snap.jsonl -Watch 600
#
#   Starting PowerShell costs ~290 ms per call (measured), so refreshing the media
#   line every 4 s used to mean "spawn a process every 4 s" and the UI had to wait
#   for it. In snapshot mode this script stays RESIDENT, writes one compact JSON
#   line into the file every few seconds, and Python just reads the last line
#   (measured: file read is well under 1 ms). Readers that want a fresh value
#   right now can still call this script one-shot -- no snapshot file, no -Watch.
#
#   The file is written as UTF-8 **without BOM**. Windows PowerShell's
#   `Out-File -Encoding UTF8` adds a BOM, which breaks naive readers; use
#   [System.IO.File]::WriteAllText with UTF8Encoding($false) instead.

param(
    [string]$Command = '',
    [string]$App = '',
    [string]$Snapshot = '',
    [int]$Watch = 0,
    [int]$IntervalMs = 3000
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$out = [ordered]@{ ok = $false; error = ''; sessions = @() }

# ---- WinRT plumbing -------------------------------------------------------
$asmPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll'
if (-not (Test-Path $asmPath)) {
    $asmPath = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\System.Runtime.WindowsRuntime.dll'
}
$asTaskDef = $null
try {
    $asm = [System.Reflection.Assembly]::LoadFrom($asmPath)
    $ext = $asm.GetType('System.WindowsRuntimeSystemExtensions')
    $asTaskDef = $ext.GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -like 'IAsyncOperation*'
    } | Select-Object -First 1
} catch {
    $out.error = "load System.Runtime.WindowsRuntime failed: $($_.Exception.Message)"
    $out | ConvertTo-Json -Depth 5 -Compress
    exit 0
}
if (-not $asTaskDef) {
    $out.error = 'AsTask<T>(IAsyncOperation<T>) not found'
    $out | ConvertTo-Json -Depth 5 -Compress
    exit 0
}

function Wait-WinRT($operation, $type) {
    $task = $script:asTaskDef.MakeGenericMethod($type).Invoke($null, @($operation))
    if (-not $task.Wait(4000)) { throw 'async operation timed out' }
    return $task.Result
}

function Read-Sessions {
    # Returns @{ sessions = ...; error = '...'; objects = ... } -- never throws.
    # `objects` keeps the real WinRT session objects (needed by TryPlayAsync etc.);
    # `sessions` is the JSON-safe snapshot data and must never contain COM objects.
    $res = @{ sessions = @(); error = ''; objects = @() }
    try {
        $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media, ContentType = WindowsRuntime]
        $mgr = Wait-WinRT ($mgrType::RequestAsync()) $mgrType
        if ($null -eq $mgr) { throw 'session manager is null' }
        $propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media, ContentType = WindowsRuntime]
        $list = @()
        foreach ($s in @($mgr.GetSessions())) {
            if ($null -eq $s) { continue }
            $res.objects += $s
            $item = [ordered]@{
                app      = [string]$s.SourceAppUserModelId
                title    = ''
                artist   = ''
                album    = ''
                status   = -1
                position = 0
                duration = 0
            }
            try {
                $mp = Wait-WinRT ($s.TryGetMediaPropertiesAsync()) $propsType
                $item.title = [string]$mp.Title
                $item.artist = [string]$mp.Artist
                $item.album = [string]$mp.AlbumTitle
            } catch {
                $item.title = ''
            }
            try { $item.status = [int]$s.GetPlaybackInfo().PlaybackStatus } catch { }
            # Progress fields are intentionally NOT parsed any more: the "watch the
            # playback position" feature was dropped by the user (title / artist /
            # playback state / next / previous are all that is needed), which is why
            # this script no longer touches TimelineProperties at all.
            $list += [pscustomobject]$item
        }
        $res.sessions = $list
    } catch {
        $res.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    }
    return $res
}

function Write-Snapshot($path, $text) {
    # Atomic write (v2): temp file + Replace, so a reader never sees a torn
    # JSON line (a torn read used to make Python fall back to the slow path).
    $enc = New-Object System.Text.UTF8Encoding($false)
    $tmp = "$path.tmp"
    [System.IO.File]::WriteAllText($tmp, $text, $enc)
    if ([System.IO.File]::Exists($path)) {
        [System.IO.File]::Replace($tmp, $path, $null)
    } else {
        [System.IO.File]::Move($tmp, $path)
    }
}

# ---- Snapshot mode: stay resident, append one JSON line every interval ----
if ($Snapshot -ne '') {
    $deadline = (Get-Date).AddSeconds($Watch)
    while ((Get-Date) -lt $deadline) {
        $one = [ordered]@{ ok = $false; error = ''; sessions = @(); ts = 0 }
        $r = Read-Sessions
        $one.sessions = $r.sessions
        if ($r.error -ne '') {
            $one.error = $r.error
        } else {
            $one.ok = $true
        }
        $one.ts = [int][double]::Parse((Get-Date -UFormat %s))
        try {
            Write-Snapshot $Snapshot ($one | ConvertTo-Json -Depth 5 -Compress)
        } catch {
            # cannot write (disk/permission): nothing useful to do, keep trying
        }
        Start-Sleep -Milliseconds $IntervalMs
    }
    exit 0
}

try {
    $r = Read-Sessions
    $out.sessions = $r.sessions
    if ($r.error -ne '') {
        $out.ok = $false
        $out.error = $r.error
        $out | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }
    $out.ok = $true
    $sessions = $r.sessions
    $objects = $r.objects

    # Optional: send one control command to one session
    # (1=play 2=pause 3=next 4=previous; the Try* methods return a bool)
    if ($Command -ne '') {
        $slot = @{ 'play' = 1; 'pause' = 2; 'next' = 3; 'previous' = 4 }[$Command]
        if ($null -eq $slot) {
            $out.command_ok = $false
            $out.command_msg = "unknown command: $Command"
        } else {
            # Match loosely: the session's SourceAppUserModelId is often a FAMILY
            # name without ".exe" (measured: Edge reports "MSEdge"), while callers
            # naturally pass the executable name ("msedge.exe"). Compare the stems
            # so both spellings work. `-App` may contain multiple comma/pipe
            # separated ids (the UI's multi-select); accept a match on any of them.
            $wants = @($App.ToLower() -split '[,|]' |
                ForEach-Object { $_ -replace '\.exe$', '' } |
                Where-Object { $_ -ne '' })
            $target = $null
            if ($wants.Count -eq 0) {
                # No app specified: prefer the session that is actually playing,
                # otherwise the first one. This mirrors the UI's selection and
                # avoids sending "pause" to a paused first session while a second
                # session is the one making sound.
                for ($i = 0; $i -lt $sessions.Count; $i++) {
                    if ([int]$sessions[$i].status -eq 4) {
                        $target = $objects[$i]
                        break
                    }
                }
                if ($null -eq $target -and $objects.Count -gt 0) {
                    $target = $objects[0]
                }
            } else {
                foreach ($s in $objects) {
                    $id = ([string]$s.SourceAppUserModelId).ToLower()
                    $idStem = $id -replace '\.exe$', ''
                    foreach ($want in $wants) {
                        if ($id.Contains($want) -or $want.Contains($idStem) -or
                            $idStem.Contains($want)) {
                            $target = $s
                            break
                        }
                    }
                    if ($null -ne $target) { break }
                }
            }
            if ($null -eq $target) {
                $out.command_ok = $false
                $out.command_msg = "no matching session (app=$App)"
            } else {
                $op = $null
                switch ($slot) {
                    1 { $op = $target.TryPlayAsync() }
                    2 { $op = $target.TryPauseAsync() }
                    3 { $op = $target.TrySkipNextAsync() }
                    4 { $op = $target.TrySkipPreviousAsync() }
                }
                $boolTask = $asTaskDef.MakeGenericMethod([bool]).Invoke($null, @($op))
                if (-not $boolTask.Wait(4000)) {
                    $out.command_ok = $false
                    $out.command_msg = 'command timed out'
                } else {
                    $out.command_ok = [bool]$boolTask.Result
                    $out.command_msg = if ($boolTask.Result) { 'accepted' } else { 'rejected' }
                }
            }
        }
    }
} catch {
    $out.ok = $false
    $out.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
}

$out | ConvertTo-Json -Depth 5 -Compress
