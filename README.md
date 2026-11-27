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
rund **1.200 Nachrichten**. Mit der Standardpause von 300 ms sind das mindestens
6 Minuten; die echte Funkzeit (und ggf. Duty-Cycle-Grenzen auf 868 MHz) kommt
noch oben drauf. Bei schlechter Verbindung oder langsamem Preset kann deutlich
mehr Zeit vergehen. Das Programm zeigt Fortschritt und Schätzung an.

| Bildgröße | Pakete (ca.) | Mindestzeit bei 300 ms Pause |
|---|---:|---:|
| 30 KB | 360 | ~2 min |
| 100 KB | 1.200 | ~6 min |
| 500 KB | 6.000 | ~30 min |

Die Pause kann mit `--gap-ms 0` auf 0 gesetzt werden; dann bestimmt die
Sendeschlange des Radios das Tempo. Für zuverlässige Übertragung lieber `--ack`
verwenden.

## Installation

Windows, PowerShell, im Projektordner:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Danach immer `.\.venv\Scripts\python.exe mcimg.py ...` aufrufen.

## Grafische Oberfläche

Die Haupt-GUI ist `mcimg_app.py` und orientiert sich an der offiziellen
MeshCore-App: links eine Navigation, dazu die Seiten **Nachrichten**,
**Kontakte**, **Bilder** und **Verbindung**. Sie verbindet direkt per BLE mit
einem Companion (z. B. Heltec V4), lädt die Kontakte und bietet zusätzlich
Bildversand und -empfang inklusive 5-Farben-Modus.

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

## 5-Farben-Modus (für kleine Displays / Heltec)

Auf Wunsch wird das Bild **in Originalauflösung** auf fünf Helligkeitsstufen
reduziert: Schwarz, drei Graustufen (25/50/75 %) und Weiß. Drei Pixel werden in
ein Byte gepackt (`wert = a*25 + b*5 + c`). Aus einer 24-Bit-RGB-Datei werden
dadurch nur noch ~0,67 Bytes pro Pixel – verkleinert wird das Bild nicht.

CLI:

```powershell
.\.venv\Scripts\python.exe mcimg.py send "C:\Fotos\IMG_0001.JPG" `
  --target "HeltecV4" --dest "Empfaenger" --palette5 --dither floyd --ack
```

In der GUI einfach **5-Farben-Modus** anhaken und ein Dither-Verfahren wählen:

- `floyd` – Floyd-Steinberg, gute Foto-Wiedergabe (Standard)
- `ordered` – 4×4-Bayer-Matrix, ruhiger, weniger „Rauschen“
- `none` – harte Quantisierung ohne Dither

Der Sender packt die Pixel in einen kleinen `MC5F`-Container (Breite, Höhe,
Dither-Modus, Pixelbytes) und verschickt ihn wie eine normale Datei. Der
Empfänger erkennt den Container automatisch und schreibt ein **Palett-PNG** mit
genau diesen fünf Farben in Originalgröße. Eine separate Option am Empfänger
ist dafür nicht nötig.

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
  --target "KameraNode" --dest "EmpfaengerNode" --ack
```

Wichtige Optionen:

| Option | Bedeutung |
|---|---|
| `--dest` | Kontaktname oder Public-Key-Prefix des Empfängers (Pflicht) |
| `--ack` | Auf die Bestätigung warten und bis zu `--retries` (3) mal erneut senden |
| `--gap-ms` | Pause zwischen Nachrichten, Standard 300 |
| `--manifest-repeat` | Manifest 3× senden, damit der Empfänger es sicher hat |
| `--limit N` | Nur N Datenpakete senden (für Tests) |
| `--report-every N` | Fortschritt alle N Pakete ausgeben |

Der Transfer ist abgeschlossen, wenn der Empfänger „EMPFANGEN: ..." meldet.
Der Sender weiß nur, dass die Pakete per ACK bestätigt wurden – die
Prüfsumme wird beim Empfänger geprüft.

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
