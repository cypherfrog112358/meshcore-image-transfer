# mcimg – vollständige Bilder über MeshCore

Sendet eine **originale Bilddatei bytegenau** über MeshCore-Direktnachrichten (DMs).
Es wird **nichts verkleinert, neu komprimiert oder umgewandelt**: Die Datei wird nur
in kleine Stücke zerlegt, Base64-kodiert und als viele normale Textnachrichten
verschickt. Ein zweites `mcimg` auf der Empfängerseite setzt die Datei wieder
zusammen und prüft den SHA-256.

Gedacht für Kameras wie die **Kodak Charmera**, deren JPEGs klein genug sind, dass
sich ein Transfer über LoRa noch sinnvoll machen lässt.

## Wichtige Realität: LoRa ist langsam

Ein MeshCore-Paket trägt hier etwa **86 Nutzbytes**. Ein 100-KB-JPEG sind damit
rund **1.200 Nachrichten**. Mit der Standardpause von 50 ms sind das mindestens
~1 Minute; die echte Funkzeit (und ggf. Duty-Cycle-Grenzen auf 868 MHz) kommt
noch oben drauf. Bei schlechter Verbindung oder langsamem Preset kann deutlich
mehr Zeit vergehen. Das Programm zeigt Fortschritt und Schätzung an.

| Bildgröße | Pakete (ca.) | Mindestzeit bei 50 ms Pause |
|---|---:|---:|
| 30 KB | 360 | ~18 s |
| 100 KB | 1.200 | ~1 min |
| 500 KB | 6.000 | ~5 min |

Drei Tempomaßnahmen sind jetzt Standard:

1. **Pause 50 ms** statt 300 ms (`--gap-ms 0` setzt sie ganz auf 0).
2. **4-Farben-Modus** mit 2 Bit/Pixel (4 Pixel/Byte) und adaptivem PNG-Container.
3. **Pipelining** mit 8 Paketen pro Block (`--pipeline`): der Empfänger meldet
   nur fehlende Pakete, genau diese werden erneut gesendet (`--repair`).

Einzel-ACK pro Paket (`--ack`) ist langsamer und deshalb standardmäßig aus.

**Sendetabelle:** Die Firmware hält nur eine begrenzte Anzahl gesendeter DMs
offen, bis der Empfänger sie auf Funk ebene bestätigt. Zu schnelles Senden
liefert `ERR_CODE_TABLE_FULL`. `mcimg` erkennt das, erhöht die Pause automatisch
auf `--table-full-wait` (Standard 1 s) und sendet das Paket erneut. Dadurch ist
eine Pause von 0 ms nicht immer schneller als 50 ms.

## Installation

Windows, PowerShell, im Projektordner:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Danach immer `.\.venv\Scripts\python.exe mcimg.py ...` aufrufen.

### Auf einem zweiten Gerät (Empfänger)

Das Repository ist **öffentlich**. Auf dem anderen Windows-Gerät einmal mit Git
klonen – eine Anmeldung ist dafür nicht nötig:

```powershell
git clone https://github.com/cypherfrog112358/meshcore-image-transfer.git
cd meshcore-image-transfer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe mcimg_app.py
```

Dort dann auf **Verbindung** den Heltec aus der Geräteliste wählen und
**Verbinden** klicken. Anschließend auf **Bilder** den Ausgabeordner setzen und
**Empfang starten**. Der Absender braucht dasselbe Tool und denselben Empfänger
als Kontakt. Es muss nur eine App gleichzeitig mit dem Heltec verbunden sein.

## Grafische Oberfläche

Die Haupt-GUI ist `mcimg_app.py` und orientiert sich an der offiziellen
MeshCore-App: links eine Navigation, dazu die Seiten **Nachrichten**,
**Kontakte**, **Bilder** und **Verbindung**. Sie verbindet direkt per BLE mit
einem Companion (z. B. Heltec V4), lädt die Kontakte und bietet zusätzlich
Bildversand und -empfang inklusive 4-Farben-Modus mit Pipeline-Reparatur.

```powershell
.\.venv\Scripts\python.exe mcimg_app.py
```

Auf dem Desktop liegt eine Verknüpfung, die genau dieses Fenster startet.
Einstellungen wie BLE-Ziel, PIN, Ausgabeordner und Dither werden lokal
gespeichert. Normale Text-DMs funktionieren im Chat-Reiter ebenfalls;
eingehende Bild-Blöcke werden dort als kompakte Hinweise angezeigt.

### BLE-Geräte finden

Auf der Seite **Verbindung** wird beim Start automatisch nach BLE-Geräten in
der Nähe gesucht. Alternativ genügt ein Klick auf **Geraete suchen**. Die
Auswahlliste zeigt **alle** gefundenen BLE-Geräte – nicht nur MeshCore – mit
Name, Adresse und Signalstärke. MeshCore-/Heltec-Geräte stehen oben.

Ein Klick auf **Verbinden** verbindet direkt mit dem ausgewählten Gerät; es
muss kein Name mehr tippen werden. Manuelle Namen oder Adressen können weiter
in das Feld geschrieben werden.

Die ältere, einfachere GUI (`mcimg_gui.py`) bleibt für Mock-Tests und als
Kommandozeilen-naher Ersatz erhalten:

```powershell
.\.venv\Scripts\python.exe mcimg_gui.py
```

## 4-Farben-Modus (für kleine Displays / Heltec)

Auf Wunsch wird das Bild **in Originalauflösung** auf vier Helligkeitsstufen
reduziert: Schwarz, zwei Graustufen (33/67 %) und Weiß. Vier Pixel werden in
ein Byte gepackt (2 Bit pro Pixel) – das sind nur noch **0,25 Bytes pro Pixel**,
25 % weniger als zuvor mit fünf Farben. Verkleinert wird das Bild nicht.

CLI:

```powershell
.\.venv\Scripts\python.exe mcimg.py send "C:\Fotos\IMG_0001.JPG" `
  --target "HeltecV4" --dest "Empfaenger" --palette4 --dither floyd
```

In der GUI einfach **4-Farben-Modus** anhaken und ein Dither-Verfahren wählen:

- `floyd` – Floyd-Steinberg, gute Foto-Wiedergabe (Standard)
- `ordered` – 4×4-Bayer-Matrix, ruhiger, weniger „Rauschen“
- `none` – harte Quantisierung, ideal für Schwarz/Weiß-Frames (z. B. Bad Apple)

Der Sender baut zwei Varianten und verschickt die **kleinere**:

- **Roh:** 2-Bit-Pixelbytes im `MC4F`-Container
- **PNG:** indiziertes PNG mit genau vier Paletteneinträgen

PNG-Filter + zlib gewinnen bei großen Flächen (Logos, Frames, Text) deutlich;
bei stark verrauschtem Floyd-Dither gewinnt meist die Rohvariante. Der Empfänger
erkennt den Modus automatisch und schreibt immer ein **Palett-PNG** in
Originalgröße. Alte `MC5F`-Container aus früheren Transfers können weiterhin
gelesen werden. Eine separate Option am Empfänger ist nicht nötig.

Der Heltec V4 wird dabei wie jeder andere MeshCore-BLE-Companion behandelt:
`--target` ist der BLE-Name/die Adresse des Heltec, `--dest` der Kontakt auf der
Empfängerseite.

## Vorbereitung

Beide Nodes müssen einander als Kontakt kennen (Advert empfangen). Kontakte
anzeigen:

```powershell
.\.venv\Scripts\python.exe mcimg.py contacts --target "MeinNode"
```

`--target` ist die BLE-Adresse oder der Advert-Name des **lokal angeschlossenen**
Companion-Radios. Ohne `--target` sucht die Bibliothek nach BLE-Geräten.
Falls ein PIN gesetzt ist: `--pin 123456`.

## Empfänger zuerst starten

```powershell
.\.venv\Scripts\python.exe mcimg.py recv --target "EmpfaengerNode" --out C:\Bilder
```

Optional:

- `--sender "KameraNode"` – nur Pakete von diesem Kontakt annehmen
- `--id f99aa697` – nur diesen Transfer annehmen
- Strg+C beendet den Empfänger; der Fortschritt bleibt erhalten

`recv` legt im Ausgabeordner versteckte Fortschrittsdateien an
(`.<id>.progress.json`). Wird der Transfer später neu gestartet und das Manifest
kommt erneut an, macht der Empfänger genau dort weiter. Nach erfolgreichem
Abschluss erscheint die Datei unter ihrem ursprünglichen Namen und die
Fortschrittsdatei wird gelöscht.

## Sender

```powershell
.\.venv\Scripts\python.exe mcimg.py send "C:\Fotos\IMG_0001.JPG" `
  --target "KameraNode" --dest "EmpfaengerNode"
```

Wichtige Optionen:

| Option | Bedeutung |
|---|---|
| `--dest` | Kontaktname oder Public-Key-Prefix des Empfängers (Pflicht) |
| `--palette4` | Bild auf 4 Farben quantisieren (adaptiv PNG/Roh) |
| `--pipeline N` | N Pakete ohne Einzel-ACK pro Block senden (Standard 8; 1 = klassisch) |
| `--repair` / `--no-repair` | Fehlende Pakete per Rückfrage erneut senden (Standard an) |
| `--repair-wait S` | Max. Wartezeit auf die Rückmeldung (Standard 30 s; auch für die Abschluss-Prüfung) |
| `--repair-rounds N` | Reparaturrunden pro Block (Standard 2) |
| `--table-full-wait S` | Pause bei voller Sendetabelle (Standard 1 s) |
| `--table-full-retries N` | Versuche bei voller Sendetabelle (Standard 10) |
| `--table-full-max-wait S` | Maximale adaptations-Pause (Standard 10 s) |
| `--ack` | Einzel-ACK pro Paket (langsam, Standard aus) |
| `--gap-ms` | Pause zwischen Nachrichten, Standard 50 |
| `--manifest-repeat` | Manifest 3× senden, damit der Empfänger es sicher hat |
| `--limit N` | Nur N Datenpakete senden (für Tests) |
| `--report-every N` | Fortschritt alle N Pakete ausgeben |

Der Transfer ist abgeschlossen, wenn der Empfänger „EMPFANGEN: ..." meldet.
Der SHA-256 bleibt das letzte Sicherheitsnetz: Stimmt er nach vollständigem
Empfang nicht, fordert der Empfänger alle Pakete erneut an. Damit das auch nach
dem letzten Block noch ankommt, fragt der Sender nach dem Versand aktiv beim
Empfänger nach und wiederholt fehlende Pakete.

Wichtig: Sender **und** Empfänger müssen diese Version verwenden, sonst kennt
der Empfänger die Rückfragen nicht. Der Sender merkt das nach einem Block und
schaltet automatisch auf Plain-Pipeline ohne Rückkanal um.

## Ohne Hardware testen

```powershell
.\.venv\Scripts\python.exe mcimg.py selftest --size 64000
```

Oder vollständiger Durchlauf über eine JSONL-Datei statt Funk:

```powershell
.\.venv\Scripts\python.exe mcimg.py send Beispiel.jpg --transport mock `
  --target C:\Temp\sim.jsonl --dest Test --gap-ms 0
.\.venv\Scripts\python.exe mcimg.py recv --transport mock `
  --target C:\Temp\sim.jsonl --out C:\Temp\empfangen
.\.venv\Scripts\python.exe -m unittest -q test_mcimg.py
```

`recv --transport mock --follow` liest die Datei laufend weiter, während
`send` schreibt.

## Protokoll

Jede Nachricht ist ASCII und höchstens 133 Zeichen lang.

```
Manifest:  I1:M:<id>:<groesse36>:<pakete36>:<sha256>:<ext>:<name_b64>
Daten:     I1:D:<id>:<seq36>:<chunk_b64>
Rückfrage: I1:Q:<id>:<von36>:<bis36>:
Antwort:   I1:R:<id>:<seq36,seq36,...>:   (oder * = alles erneut)
```

- `id` = erste 8 Hex-Zeichen des SHA-256 der Datei (stabil bei Neustarts)
- Zahlen sind Base36 ohne führende Nullen
- Base64 ohne `=`-Padding
- Die Manifest-Nachricht enthält Name, Endung, Gesamtgröße, Paketanzahl und
  vollständigen SHA-256
- Der Empfänger verwirft Duplikate, akzeptiert beliebige Reihenfolge, speichert
  den Fortschritt und gibt die Datei erst frei, wenn alle Pakete da sind und
  der SHA-256 stimmt

Die Datei `mcimg_protocol.py` enthält Kodierung und Parser; `mcimg.py` die CLI,
Radioschnittstelle und den Empfänger. Es werden keine Bibliotheken zum
Dekodieren von JPEG/PNG benötigt – die Bytes bleiben unverändert.

## Grenzen

- Nur DMs an einen Kontakt. Kanal-Datagramme wären schneller (~165 Bytes pro
  Paket), aber Broadcast und brauchen einen anderen Empfängerpfad.
- Die offizielle MeshCore-App kann die Teile **nicht** automatisch zusammenbauen;
  Empfänger und Sender müssen dieses Tool verwenden.
- MeshCore begrenzt Textnachrichten auf 133 Zeichen. Falls die Firmware weniger
  zulässt: `--max-chars` kleiner setzen (z. B. 120).
- Die BLE-Verbindung zum Companion-Radio kann getrennt werden;
  `--auto-reconnect` hilft dabei.

### Fehler `ERR_CODE_BAD_STATE` beim Laden der Kontakte

Die App sendet Kontakt- und Nachrichten-Kommandos jetzt nacheinander und
versucht das Laden der Kontakte bis zu viermal (mit kurzem `APP_START`-Reset
dazwischen). Bleibt der Fehler, ist meist noch eine andere Anwendung mit dem
Companion verbunden: BLE erlaubt nur **eine** aktive Verbindung. Die offizielle
MeshCore-App (oder ein anderes Tool) muss getrennt werden, danach in unserer
App erneut auf **Verbinden** klicken. Falls unsere App bereits verbunden ist,
hilft **Trennen** und erneutes **Verbinden**.
