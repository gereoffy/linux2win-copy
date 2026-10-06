<#
lnx2win_recv.ps1 - fajlok lehuzasa az lnx2win_send.py-tol (Windows oldal).

Fuggoseg: csak a beepitett Windows PowerShell 5.1 (Windows 10/11, Server 2016+).
Telepites/konfiguralas nem kell; a Windows kifele kapcsolodik, igy a Windows
tuzfalhoz sem kell nyulni.

Hasznalat:
  powershell -ExecutionPolicy Bypass -File lnx2win_recv.ps1 -Server 192.168.1.10 -Token abcd1234 -Dest D:\cel

Ujrafuttatva csak a hianyzo / eltero meretu vagy datumu fajlokat keri le
(-All kapcsoloval mindent ujra).
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Token,
    [Parameter(Mandatory = $true)][string]$Dest,
    [int]$Port = 50505,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$utf8 = New-Object Text.UTF8Encoding($false)
$epoch = 621355968000000000L          # 1970-01-01 UTC, .NET tick-ben
$tolerance = 20000000L                # 2 s (FAT/exFAT pontossag miatt)
$errLog = Join-Path (Get-Location) 'lnx2win_errors.log'
$errors = 0

function Log-Error([string]$msg) {
    $script:errors++
    Write-Host ("`nHIBA: " + $msg) -ForegroundColor Red
    [IO.File]::AppendAllText($script:errLog, $msg + "`r`n", $script:utf8)
}

function Read-Exact([IO.Stream]$s, [byte[]]$b, [int]$n) {
    $off = 0
    while ($off -lt $n) {
        $r = $s.Read($b, $off, $n - $off)
        if ($r -le 0) { throw 'A kapcsolat megszakadt.' }
        $off += $r
    }
}

function Fmt([double]$n) {
    foreach ($u in 'B', 'KB', 'MB', 'GB') { if ($n -lt 1024) { return ('{0:N1} {1}' -f $n, $u) }; $n /= 1024 }
    return ('{0:N2} TB' -f $n)
}

# --- Celkonyvtar, hosszu utvonal (\\?\) tamogatassal ---
$destFull = [IO.Path]::GetFullPath($Dest).TrimEnd('\')
if ($destFull.StartsWith('\\')) { $root = '\\?\UNC\' + $destFull.Substring(2) } else { $root = '\\?\' + $destFull }
try {
    [void][IO.Directory]::CreateDirectory($root + '\')
    [void][IO.Directory]::Exists($root)
} catch {
    Write-Warning 'A \\?\ hosszu utvonal nem tamogatott, 260 karakternel hosszabb utvonalak hibat adhatnak.'
    $root = $destFull
    [void][IO.Directory]::CreateDirectory($root + '\')
}

# --- Kapcsolodas ---
Write-Host "Kapcsolodas: ${Server}:$Port ..."
$client = New-Object Net.Sockets.TcpClient
$client.ReceiveBufferSize = 4MB
$client.Connect($Server, $Port)
$ns = $client.GetStream()
$bs = New-Object IO.BufferedStream($ns, 1MB)

$tok = $utf8.GetBytes($Token)
$hello = [byte[]]([Text.Encoding]::ASCII.GetBytes('L2W1') + [BitConverter]::GetBytes([uint32]$tok.Length) + $tok)
$ns.Write($hello, 0, $hello.Length)

$b = New-Object byte[] 64
Read-Exact $bs $b 2
if ([Text.Encoding]::ASCII.GetString($b, 0, 2) -ne 'OK') { throw 'A szerver elutasitotta a tokent.' }

# --- Manifest ---
Write-Host 'Fajllista fogadasa (a szerver most jarja be a konyvtarat)...'
Read-Exact $bs $b 4
$count = [int][BitConverter]::ToUInt32($b, 0)
$isDir = New-Object bool[] $count
$sizes = New-Object long[] $count
$mtimes = New-Object long[] $count
$paths = New-Object string[] $count
$pb = New-Object byte[] 65536
for ($i = 0; $i -lt $count; $i++) {
    Read-Exact $bs $b 21
    $isDir[$i] = ($b[0] -eq 1)
    $sizes[$i] = [BitConverter]::ToInt64($b, 1)
    $mtimes[$i] = [BitConverter]::ToInt64($b, 9)
    $len = [int][BitConverter]::ToUInt32($b, 17)
    if ($len -gt $pb.Length) { $pb = New-Object byte[] $len }
    Read-Exact $bs $pb $len
    $paths[$i] = $utf8.GetString($pb, 0, $len)
}
Write-Host "$count bejegyzes. Konyvtarak letrehozasa es meglevo fajlok ellenorzese..."

# --- Mit kell lehuzni? ---
$ms = New-Object IO.MemoryStream
$bw = New-Object IO.BinaryWriter($ms)
$bw.Write([uint32]0)
$need = 0; $needBytes = 0L; $skip = 0
for ($i = 0; $i -lt $count; $i++) {
    $p = $root + '\' + $paths[$i]
    if ($isDir[$i]) {
        try { [void][IO.Directory]::CreateDirectory($p) } catch { Log-Error ("mkdir " + $paths[$i] + ": " + $_.Exception.Message) }
        continue
    }
    if (-not $All) {
        $fi = [IO.FileInfo]::new($p)
        if ($fi.Exists -and $fi.Length -eq $sizes[$i] -and
            [Math]::Abs($fi.LastWriteTimeUtc.Ticks - ($epoch + $mtimes[$i])) -le $tolerance) { $skip++; continue }
    }
    $bw.Write([uint32]$i)
    $need++; $needBytes += $sizes[$i]
}
$bw.Flush()
$req = $ms.ToArray()
[Array]::Copy([BitConverter]::GetBytes([uint32]$need), 0, $req, 0, 4)
$ns.Write($req, 0, $req.Length)
Write-Host ("Mar meglevo (kihagyva): $skip fajl. Lehuzando: $need fajl, " + (Fmt $needBytes))

# --- Fajlok fogadasa ---
$buf = New-Object byte[] 1MB
$sw = [Diagnostics.Stopwatch]::StartNew()
$nextReport = 1000
$doneBytes = 0L; $doneFiles = 0
while ($true) {
    Read-Exact $bs $b 12
    $idx = [BitConverter]::ToUInt32($b, 0)
    $size = [BitConverter]::ToInt64($b, 4)
    if ($idx -eq [uint32]::MaxValue) { break }
    if ($size -lt 0) { Log-Error ("a szerver nem tudta olvasni: " + $paths[$idx]); continue }

    $p = $root + '\' + $paths[$idx]
    $fs = $null
    try { $fs = [IO.FileStream]::new($p, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None, 1MB) }
    catch { Log-Error ("nem irhato: " + $paths[$idx] + ": " + $_.Exception.Message) }

    $rem = $size
    try {
        while ($rem -gt 0) {
            $n = $buf.Length
            if ($rem -lt $n) { $n = [int]$rem }
            $r = $bs.Read($buf, 0, $n)
            if ($r -le 0) { throw 'A kapcsolat megszakadt.' }
            if ($fs) { $fs.Write($buf, 0, $r) }
            $rem -= $r; $doneBytes += $r
            if ($sw.ElapsedMilliseconds -ge $nextReport) {
                $nextReport = $sw.ElapsedMilliseconds + 1000
                $sec = $sw.Elapsed.TotalSeconds
                $rate = $doneBytes / $sec
                $eta = if ($rate -gt 0) { [TimeSpan]::FromSeconds([Math]::Min([Math]::Floor(($needBytes - $doneBytes) / $rate), 31536000)) } else { '?' }
                [Console]::Write(("`r{0}/{1} fajl  {2} / {3}  {4}/s  hatravan: {5}      " -f
                        $doneFiles, $need, (Fmt $doneBytes), (Fmt $needBytes), (Fmt $rate), $eta))
            }
        }
    } finally {
        if ($fs) { $fs.Dispose() }
    }
    if ($fs) {
        try { [IO.File]::SetLastWriteTimeUtc($p, (New-Object DateTime(($epoch + $mtimes[$idx]), [DateTimeKind]::Utc))) }
        catch { Log-Error ("datum beallitasa: " + $paths[$idx] + ": " + $_.Exception.Message) }
    }
    $doneFiles++
}
$client.Close()

# Konyvtarak datuma (alulrol felfele, hogy a fajlirasok ne irjak felul)
for ($i = $count - 1; $i -ge 0; $i--) {
    if ($isDir[$i]) {
        try { [IO.Directory]::SetLastWriteTimeUtc($root + '\' + $paths[$i], (New-Object DateTime(($epoch + $mtimes[$i]), [DateTimeKind]::Utc))) } catch {}
    }
}

$sec = [Math]::Max($sw.Elapsed.TotalSeconds, 0.001)
Write-Host ("`nKesz: $doneFiles fajl, " + (Fmt $doneBytes) + (" {0:N0} s alatt (" -f $sec) + (Fmt ($doneBytes / $sec)) + "/s).")
if ($errors) { Write-Host "$errors hiba, reszletek: $errLog" -ForegroundColor Yellow }
