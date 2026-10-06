#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lnx2win_send.py - konyvtarfa kiszolgalasa Windows-ra masolashoz (Linux oldal).

Fuggoseg: csak python3 (3.6+), semmi mas.
A Windows oldalon az lnx2win_recv.ps1 kapcsolodik ide es huzza le a fajlokat.

Hasznalat:
    python3 lnx2win_send.py /utvonal/a/konyvtarhoz [--port 50505] [--token valami]

A fajlneveket Windows-kompatibilisse alakitja (UTF-8 -> NFC, tiltott karakterek,
foglalt nevek, kis/nagybetu utkozesek), es minden atnevezest kiir.
Tobbszor is lehet csatlakozni: a kliens csak a hianyzo/eltero fajlokat keri le,
igy megszakadt masolas folytathato.
"""
import argparse
import hmac
import os
import secrets
import socket
import stat
import struct
import sys
import time
import unicodedata

MAGIC = b"L2W1"
END_INDEX = 0xFFFFFFFF
SMALL_FILE = 256 * 1024        # ennel kisebb fajlokat pufferelve kuldunk
FLUSH_AT = 4 * 1024 * 1024     # puffer meret, amikor kuldunk

INVALID_CHARS = set('<>:"\\|?*') | {chr(i) for i in range(32)}
RESERVED = ({"CON", "PRN", "AUX", "NUL"}
            | {"COM%d" % i for i in range(1, 10)}
            | {"LPT%d" % i for i in range(1, 10)})


def log(msg):
    # Fajlnevek miatt mindig UTF-8-ban irunk, a terminal locale-jatol fuggetlenul.
    sys.stdout.buffer.write((msg + "\n").encode("utf-8", "backslashreplace"))
    sys.stdout.buffer.flush()


def decode_name(raw, fallback):
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode(fallback, "replace")
    return unicodedata.normalize("NFC", s)


def win_safe(name):
    s = "".join("_" if c in INVALID_CHARS else c for c in name)
    stripped = s.rstrip(" .")
    if stripped != s:
        s = stripped + "_" * (len(s) - len(stripped))
    if s.split(".")[0].rstrip(" ").upper() in RESERVED:
        s = "_" + s
    return s


def unique_name(name, used, taken):
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    n = 2
    while True:
        cand = "%s (%d)%s" % (stem, n, ext)
        key = cand.upper()
        if key not in used and key not in taken:
            return cand
        n += 1


class Entry(object):
    __slots__ = ("is_dir", "src", "rel", "size", "mtime")

    def __init__(self, is_dir, src, rel, size, mtime):
        self.is_dir, self.src, self.rel, self.size, self.mtime = is_dir, src, rel, size, mtime


def scan(root, fallback):
    """Bejarja a fat. Visszaadja az Entry listat (konyvtar mindig a tartalma elott)."""
    entries = []
    renamed = skipped = 0
    stack = [(os.fsencode(root), "")]
    while stack:
        d, rel = stack.pop()
        try:
            items = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as ex:
            log("HIBA (konyvtar kihagyva): %r: %s" % (d, ex))
            skipped += 1
            continue
        names = [decode_name(e.name, fallback) for e in items]
        safes = [win_safe(n) for n in names]
        taken = {s.upper() for s in safes}
        used = set()
        subdirs = []
        for e, name, safe in zip(items, names, safes):
            try:
                if e.is_symlink():
                    log("KIHAGYVA (symlink): %s" % (rel + "/" + name).lstrip("/"))
                    skipped += 1
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError as ex:
                log("HIBA (kihagyva): %r: %s" % (e.path, ex))
                skipped += 1
                continue
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                log("KIHAGYVA (specialis fajl): %s" % (rel + "/" + name).lstrip("/"))
                skipped += 1
                continue
            if safe.upper() in used:
                safe = unique_name(safe, used, taken)
            used.add(safe.upper())
            new_rel = (rel + "\\" + safe) if rel else safe
            if safe != name or e.name != name.encode("utf-8"):
                renamed += 1
                log("ATNEVEZVE: %s  ->  %s" % ((rel.replace("\\", "/") + "/" + name).lstrip("/"),
                                                new_rel.replace("\\", "/")))
            mtime = st.st_mtime_ns // 100
            if stat.S_ISDIR(st.st_mode):
                entries.append(Entry(True, e.path, new_rel, 0, mtime))
                subdirs.append((e.path, new_rel))
            else:
                entries.append(Entry(False, e.path, new_rel, st.st_size, mtime))
        stack.extend(reversed(subdirs))
    return entries, renamed, skipped


def recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise ConnectionError("a kliens bontotta a kapcsolatot")
        buf += chunk
    return bytes(buf)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def serve_client(conn, args):
    if recv_exact(conn, 4) != MAGIC:
        raise ConnectionError("ismeretlen protokoll")
    (tlen,) = struct.unpack("<I", recv_exact(conn, 4))
    if tlen > 1024 or not hmac.compare_digest(recv_exact(conn, tlen), args.token.encode()):
        conn.sendall(b"NO")
        raise ConnectionError("hibas token")
    conn.sendall(b"OK")

    log("Konyvtar bejarasa: %s" % args.source)
    t0 = time.time()
    entries, renamed, skipped = scan(args.source, args.fallback_encoding)
    nfiles = sum(1 for e in entries if not e.is_dir)
    total = sum(e.size for e in entries)
    log("Bejarva %.1f s alatt: %d konyvtar, %d fajl, %s, %d atnevezve, %d kihagyva"
        % (time.time() - t0, len(entries) - nfiles, nfiles, human(total), renamed, skipped))

    # Manifest
    buf = bytearray(struct.pack("<I", len(entries)))
    for e in entries:
        p = e.rel.encode("utf-8")
        buf += struct.pack("<BqqI", 1 if e.is_dir else 0, e.size, e.mtime, len(p))
        buf += p
        if len(buf) >= FLUSH_AT:
            conn.sendall(buf)
            buf = bytearray()
    conn.sendall(buf)

    # Kliens altal kert fajlok
    (n,) = struct.unpack("<I", recv_exact(conn, 4))
    wanted = struct.unpack("<%dI" % n, recv_exact(conn, 4 * n)) if n else ()
    want_bytes = sum(entries[i].size for i in wanted)
    log("A kliens %d fajlt ker (%s)" % (n, human(want_bytes)))

    t0 = time.time()
    sent_bytes = 0
    last = t0
    buf = bytearray()
    for i in wanted:
        e = entries[i]
        try:
            f = open(e.src, "rb")
        except OSError as ex:
            log("HIBA (nem olvashato): %s: %s" % (e.rel, ex))
            buf += struct.pack("<Iq", i, -1)
            continue
        with f:
            size = os.fstat(f.fileno()).st_size
            buf += struct.pack("<Iq", i, size)
            if size <= SMALL_FILE:
                data = f.read(size)
                if len(data) < size:
                    log("FIGYELEM (kozben valtozott): %s" % e.rel)
                    data += b"\0" * (size - len(data))
                buf += data
                if len(buf) >= FLUSH_AT:
                    conn.sendall(buf)
                    buf = bytearray()
            else:
                if buf:
                    conn.sendall(buf)
                    buf = bytearray()
                done = conn.sendfile(f, 0, size)
                if done < size:
                    log("FIGYELEM (kozben valtozott): %s" % e.rel)
                    pad = size - done
                    while pad:
                        k = min(pad, 1 << 20)
                        conn.sendall(b"\0" * k)
                        pad -= k
        sent_bytes += size
        now = time.time()
        if now - last >= 5:
            last = now
            log("  %s / %s  (%.1f MB/s)" % (human(sent_bytes), human(want_bytes),
                                            sent_bytes / (now - t0) / 1e6))
    buf += struct.pack("<Iq", END_INDEX, 0)
    conn.sendall(buf)
    dt = max(time.time() - t0, 0.001)
    log("Kesz: %s elkuldve %.0f s alatt (%.1f MB/s)" % (human(sent_bytes), dt, sent_bytes / dt / 1e6))


def guess_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # nem kuld csomagot, csak utvonalat valaszt
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "<ennek-a-gepnek-az-IP-je>"


def main():
    ap = argparse.ArgumentParser(description="Konyvtarfa kiszolgalasa lnx2win_recv.ps1 szamara")
    ap.add_argument("source", help="a masolando konyvtar")
    ap.add_argument("--port", type=int, default=50505)
    ap.add_argument("--bind", default="0.0.0.0", help="figyelt cim (alap: minden interfesz)")
    ap.add_argument("--token", default=None, help="jelszo a klienshez (alap: veletlen)")
    ap.add_argument("--fallback-encoding", default="iso-8859-2",
                    help="nem UTF-8 fajlnevek kodolasa (alap: iso-8859-2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="csak bejaras + atnevezesek listazasa, nincs halozat")
    args = ap.parse_args()
    args.source = os.path.abspath(args.source)
    if not os.path.isdir(args.source):
        ap.error("nem konyvtar: %s" % args.source)

    if args.dry_run:
        entries, renamed, skipped = scan(args.source, args.fallback_encoding)
        nfiles = sum(1 for e in entries if not e.is_dir)
        log("%d konyvtar, %d fajl, %s, %d atnevezve, %d kihagyva"
            % (len(entries) - nfiles, nfiles, human(sum(e.size for e in entries)), renamed, skipped))
        return

    args.token = args.token or secrets.token_hex(4)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    log("Varakozas a Windows kliensre a %d porton. A Windows gepen futtasd:" % args.port)
    log("")
    log("  powershell -ExecutionPolicy Bypass -File lnx2win_recv.ps1 -Server %s -Port %d -Token %s -Dest D:\\cel"
        % (guess_ip(), args.port, args.token))
    log("")
    log("(Ctrl+C a leallitashoz. Megszakadt masolas utan ugyanazzal a paranccsal folytathato.)")
    while True:
        conn, addr = srv.accept()
        log("Kapcsolodott: %s:%d" % addr)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        try:
            serve_client(conn, args)
        except (OSError, ConnectionError) as ex:
            log("Kapcsolat vege hibaval: %s" % ex)
        finally:
            conn.close()
        log("Ujra varakozas kliensre...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nLeallitva.")
