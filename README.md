# lnx2win

Nagy könyvtárfa gyors másolása Linuxról Windowsra, ékezetes fájlnevekkel együtt.
Egyik gépre sem kell semmit telepíteni vagy konfigurálni.

| Oldal   | Fájl               | Követelmény                               |
|---------|--------------------|-------------------------------------------|
| Linux   | `lnx2win_send.py`  | `python3` (3.6+)                          |
| Windows | `lnx2win_recv.ps1` | beépített PowerShell 5.1 (Win 10/11, Server 2016+) |

## Használat

**1. Linuxon** indítsd el a kiszolgálót:

```bash
python3 lnx2win_send.py /utvonal/a/konyvtarhoz
```

Kiírja a Windowson futtatandó parancsot (IP-címmel és egy véletlen tokennel).

**2. Windowson** futtasd a kiírt parancsot, a `-Dest` értékét a célkönyvtárra cserélve:

```
powershell -ExecutionPolicy Bypass -File lnx2win_recv.ps1 -Server 192.168.1.10 -Token ab12cd34 -Dest D:\cel
```

A `-ExecutionPolicy Bypass` csak erre az egy futtatásra érvényes, nem állít át semmit.

**3. Folytatás / ellenőrzés:** ha a másolás megszakad, futtasd újra ugyanazt a Windows-parancsot.
Csak a hiányzó vagy méretben/dátumban eltérő fájlokat kéri le. Ha a végén
„Lehúzandó: 0 fájl” jelenik meg, minden fájl átjött.

A Linux oldalt `Ctrl+C`-vel lehet leállítani.

## Kapcsolók

**`lnx2win_send.py`**

| Kapcsoló | Jelentés |
|---|---|
| `--port N` | port (alap: 50505) |
| `--token X` | saját token a véletlen helyett |
| `--bind CÍM` | csak ezen a címen figyel (alap: minden interfész) |
| `--fallback-encoding E` | a nem UTF-8 fájlnevek kódolása (alap: `iso-8859-2`) |
| `--dry-run` | csak bejárja a fát és kilistázza az átnevezéseket, hálózat nélkül |

**`lnx2win_recv.ps1`**

| Kapcsoló | Jelentés |
|---|---|
| `-Server`, `-Token`, `-Dest` | kötelezők |
| `-Port N` | port (alap: 50505) |
| `-All` | mindent újra lehúz, a meglévő fájlokat is |

## Fájlnevek

A Windowson nem használható neveket a Linux oldal alakítja át, és minden átnevezést kiír
(`ATNEVEZVE: régi -> új`):

- UTF-8 → NFC normalizálás; a nem UTF-8 neveket a `--fallback-encoding` szerint olvassa
- a `< > : " \ | ? *` és a vezérlőkarakterek, valamint a záró pont/szóköz helyére `_` kerül
- a foglalt nevek (`CON`, `NUL`, `COM1`…) elé `_` kerül
- kis/nagybetű ütközés esetén: `Ő.txt` → `Ő (2).txt`

A 260 karakternél hosszabb útvonalak is működnek. A symlinkeket és a speciális fájlokat
kihagyja (ezeket is kiírja). A módosítási dátumok megmaradnak.

## Tudnivalók

- **Nincs titkosítás**, csak a token véd: megbízható helyi hálózaton használd.
- A Windows kapcsolódik kifelé, így a Windows tűzfalhoz nem kell nyúlni. Ha a Linux
  tűzfala blokkolja a portot, válassz nyitott portot a `--port` kapcsolóval.
- A Windows Defender valós idejű vizsgálata sok kis fájlnál lassíthat.
- A Windows oldali hibák az aktuális könyvtárban, az `lnx2win_errors.log` fájlban gyűlnek.
- Sebesség: egyetlen TCP-folyam, fájlonként nincs oda-vissza üzenetváltás. Gigabites
  hálózaton ~100 MB/s várható (600 GB ≈ 1,5–2 óra).
