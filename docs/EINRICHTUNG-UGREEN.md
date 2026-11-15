# Taktgefühl auf einem UGREEN-NAS (UGOS Pro)

Ohne Kommandozeile, nur über die Docker-App von UGOS. Auf anderen NAS mit Docker-Oberfläche (Synology Container Manager, QNAP Container Station) läuft es sinngemäß gleich.

## 1. Image holen

1. **Docker → Image → Lokal → Image hinzufügen → Von Paketquelle**
2. **Nach Imagename** wählen und den vollen Namen eintragen: `ghcr.io/hendrikheidsiek/taktgefuehl`, Version: `latest`
3. **Bestätigen**.

„Nach URL“ nimmt nur Links zu Docker Hub; für Images von ghcr.io immer den Imagenamen verwenden.

## 2. Container erstellen

Beim Image auf **+** klicken.

- **Containername:** `taktgefuehl`, **Automatischer Neustart:** an
- **Port:** einen freien NAS-Port (z. B. `8390`) auf Container-Port `8080`

**Volume**

| NAS-Ordner | Pfad im Container | Rechte |
|---|---|---|
| ein eigener Ordner, z. B. `docker/taktgefuehl/data` | `/data` | Lesen/Schreiben |

`/data` enthält die Datenbank mit deinem gesammelten Verlauf, den Phasen und Mixen sowie die Zugänge zu YouTube und Anthropic. Diesen Ordner ins Backup aufnehmen: Der Verlauf lässt sich nicht von YouTube zurückholen.

Achtung beim Ordner-Auswählen: UGOS übernimmt den Ordner, der im Dialog **geöffnet** ist, nicht die markierte Zeile. Also in den Zielordner hineinklicken und dann bestätigen. Danach mit der Maus auf den Pfad zeigen und prüfen.

**Umgebungsvariablen** (vorbelegt, nur anpassen)

- `ADMIN_PASSWORD`: UGOS akzeptiert keine leeren Werte. Entweder ein Passwort eintragen oder die Zeile mit ⊖ entfernen; dann steht ein Zufallspasswort einmalig im Container-Protokoll. **Sofort notieren.**
- `ANTHROPIC_API_KEY`: leer lassen geht nicht, also entweder den Key eintragen oder die Zeile entfernen und den Key später in der App eintragen.
- `TZ`: `Europe/Berlin` oder deine Zeitzone
- `PUID` / `PGID`: von UGOS vorbelegt, so lassen
- `PORT`, `DATA_PATH`, `MODEL_PATH`: nicht ändern

Alle weiteren Einstellungen stehen in der Tabelle im [README](../README.md#einstellungen-umgebungsvariablen).

## 3. Prüfen

- Browser: `http://<NAS-IP>:8390/api/health` → `{"ok":true,…}`
- `http://<NAS-IP>:8390/` zeigt die Startseite. Die App liegt im fertigen Image nicht bei; wie du sie baust und über den Server verteilst, steht im README unter „App bauen“.

## 4. Updates

**Docker → Container → taktgefuehl → Aktualisieren.** UGOS lädt die neueste Version des Images und startet den Container mit denselben Einstellungen neu. Die Daten in `/data` bleiben erhalten.
