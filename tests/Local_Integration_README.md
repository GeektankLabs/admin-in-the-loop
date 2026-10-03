# Admin in the Loop – MVP 0.8

**AI assists, Admin decides.**

Command-Vorschläge und ausgewählte Terminalausgabe werden vor jeder Übergabe vom Benutzer geprüft.

Stand des beschriebenen MVP: 30. September 2026. Die bisherige lokale Integration wurde um Bridge, MCP, OpenAI-Tunnelsteuerung und Remote-Kopplung erweitert. Die damals getroffenen GUI-Entscheidungen sind umgesetzt. Diese Anleitung ersetzt die frühere Offline-Anleitung. Produkt-/Konzeptentscheidungen stehen in [`doc/Admin-in-the-Loop_Konzept_V0.7.md`](../doc/Admin-in-the-Loop_Konzept_V0.7.md); alle aktive Implementierungs-, Validierungs- und Abnahmearbeit wird ausschließlich im Taskboard geführt. Diese Anleitung und die historischen Status-/Vorschlagsdateien sind keine parallelen Todo-Listen.

## Dateien und Architektur

| Datei | Aufgabe |
|---|---|
| `admin_in_the_loop.py` | Einziger Python-Anwendungseinstieg: Controller, GUI, PTY-Helfer, Netzwerkclient, Tunnelsteuerung, Bridge-only-Modus und 49 bestehende Selbsttests |
| `test_connected_mvp.py` | HTTP-, MCP-, TLS-, PTY-, Wiederverbindungs- und Tunneltests |
| `gui_smoke_test.py` | Ein echter Tkinter-/xterm-Test unter einem virtuellen Linux-Display |
| `../doc/Admin-in-the-Loop_Konzept_V0.7.md` | Maßgebliche Produkt-/Konzeptreferenz; keine laufende Aufgabenliste |
| `../IMPLEMENTATION_STATUS.md` | Historischer Implementierungs-/Evidenzstand vom 30. September 2026; kein aktiver Backlog |
| `../doc/TASKBOARD_PROPOSAL.md` | Historische Migrationsrationale und Ticket-ID-Querverzeichnis; kein aktiver Backlog |

Controller und Bridge sind in **einer herunterladbaren Python-Anwendungsdatei** gebündelt. `--bridge` startet ausschließlich den headless Transportdienst. Im lokalen Profil startet die GUI dieselbe Datei als separaten Prozess; dieser erhält nur die ausdrücklich übergebenen privaten Bootstrap-/Ready-Pipes, eine allowlist-Umgebung und keine Terminal-/IPC-Deskriptoren. Der Bridge-Prozess startet keine Shell und besitzt keine Controller-API. Ein einzelner Download bedeutet daher nicht einen einzigen Prozess. Der offizielle OpenAI-Tunnel-Client ist ein separat installiertes Programm.

Die GUI und lokale Tests verwenden denselben `AppCore`. Das ist eine lokale Programmschnittstelle, kein Netzwerkdienst mit Freigabe- oder Terminalrechten. Die Bridge kennt keine Aktion, mit der die AI eine menschliche Freigabe auslösen könnte.

## Voraussetzungen und Start

Unix, Python **3.11 oder neuer**, Bash. Debian-Pakete für den Controller mit GUI:

```bash
sudo apt install python3 python3-tk xterm
python3 admin_in_the_loop.py
```

`python3 admin_in_the_loop.py --gui` ist weiterhin ein gültiger expliziter GUI-Start.

Für Remote-Kopplung auf dem Controller und TLS-Zertifikatserstellung auf der Bridge zusätzlich:

```bash
sudo apt install python3-cryptography
```

Es gibt keine automatische Paketinstallation, keinen vorgeschriebenen Container und keine externen Python-Testbibliotheken. Der lokale Controller einschließlich seiner 49 Tests nutzt die Standardbibliothek. Der getrennte Netzwerkdienst (Bridge/MCP) nutzt ebenfalls die Standardbibliothek; Remote-TLS-Zertifikatsgenerierung verwendet `cryptography`. Der Tunnel-Client muss separat aus einer offiziellen OpenAI-Distribution installiert werden; er ist nicht im Downloadpaket enthalten.

macOS: Python ab 3.11 mit Tkinter und Terminal.app; vorhandene Apple-Python-Installationen müssen diese Voraussetzungen tatsächlich erfüllen. Die native Terminalsteuerung nutzt `/usr/bin/osascript` und kann eine Automation-Erlaubnis benötigen. Ein Homebrew-Zwang besteht nicht. Netzwerkfunktionen benötigen auch dort die genannten kleinen Laufzeitbibliotheken in der gewählten Python-Installation; dafür wurde kein eigener Installer gebaut. Alternativ kann die Bridge auf einem Debian-Gateway laufen. macOS ist in dieser Umgebung nicht praktisch abgenommen.

Ohne Option startet die GUI. Importieren des Moduls startet weder Terminal, GUI noch Netzwerk. Der headless Dienst startet ausschließlich mit `--bridge`; die GUI startet eine lokale Bridge nur nach einer bewussten Benutzeraktion.

Das Controllerfenster startet groß im Querformat. **F11** schaltet Vollbild, **Esc** beendet Vollbild. Das echte Terminal bleibt ein separates Systemterminalfenster. Ein eingebetteter Terminalrenderer oder automatische Fensteranordnung gehört nicht zu dieser Umsetzung.

## Oberfläche und lokaler Probelauf

- Links Terminalstatus und Ausgabeauswahl; in der Mitte der große Review; rechts AI-/MCP-Verbindung und Vorschlagsstatus. Gerahmte Arbeitsbereiche mit ungefähr **1:2:1** Breitenverhältnis; die Seitenbereiche sind gleich gestaltet, der mittlere Review ist der primäre Arbeitsbereich.
- „Terminal öffnen“ und „Verbindung einstellen“ stehen oben auf gleicher Höhe. Beide Prüfen-Buttons und Freigeben/Verwerfen stehen unten auf einer gemeinsamen Zeile.
- Keine Inhaltsvorschau in den Seitenbereichen. Grün signalisiert einen vorhandenen Vorschlag, Orange einen leeren Platz. Die Farbe bewertet keine Sicherheit. Erst „Prüfen“ übernimmt den Vorschlag in den Review und leert den lokalen Platz.
- Der Review hat weiche Zeilenumbrüche und ausschließlich eine vertikale Scrollbar. Die Umbrüche verändern den übertragenen Text nicht.
- „Finales Return senden“ erscheint nur bei Commands. Beim ersten Start eingeschaltet, danach persistent nach Benutzerwahl. Interne Zeilenumbrüche können schon ohne finales Return Befehle ausführen; die Oberfläche weist darauf hin.
- Blockgrößen **SINGLE, SMALL, MEDIUM, LARGE**; persistente Komfortpräferenz, keine Ausführungsberechtigung.
- Ausgabeauswahl: **Letzte Zeile**, **Seit letztem Command**, **Letzte 20**, **Letzte 50**, **Letzte 100**, **Letzte 500**. Beim ersten Öffnen/Ausprobieren sind **Letzte 20** ausgewählt, damit unmittelbarer Terminal-Output sichtbar ist. Nach dem ersten bestätigten Transfer wechselt die Auswahl zu „Seit letztem Command“; danach bleibt die eigene Auswahl bestehen.
- Solange die Shell erfolgreich am Controller hängt, lautet der Status „Terminal verbunden“ und der nicht mehr benötigte Button „Terminal öffnen“ ist ausgeblendet. Während der Shell-Verbindung erscheint „Terminal verbindet“ und der Button ist deaktiviert. Nach Ende/Verlust erscheint „Terminal getrennt“; „Terminal öffnen“ legt für den nächsten Attach einen frischen einmaligen IPC-Endpunkt an.

Probelauf ohne AI:

1. GUI in einem unkritischen Arbeitsverzeichnis starten, „Terminal öffnen“ wählen und auf „Verbunden“ warten. Die innere Bash verwendet keine Benutzerprofile und keine Shell-History.
2. „Verbindung einstellen“ → „Weitere Optionen / lokaler Test“ aufklappen → „Offline-Testvorschlag laden“. Dafür muss die Verbindung getrennt und der Vorschlagsplatz leer sein. Beim Programmstart liegt noch kein Vorschlag vor.
3. Rechts „← Prüfen“, den Text in der Mitte prüfen, anschließend „← Freigeben ins Terminal“. Bei unbekanntem Prompt ist eine weitere Bestätigung erforderlich.
4. Nach dem Transfer links Ausgabe wählen und „Prüfen →“. Eine andere Auswahl lässt sich erneut in den Review übernehmen.
5. Erst „Freigeben an AI →“ gibt genau diesen Snapshot frei. Ohne Bridge wird lediglich die lokale Testfreigabe vermerkt; es erfolgt keine Anbieterübertragung.

Textbearbeitung über das Kontextmenü des Reviews oder **Ctrl+E**, auf macOS zusätzlich **Cmd+E**. Die Übernahme aus dem Editor erzeugt eine neue Review-ID. Die Hauptfläche bleibt ein fester, schreibgeschützter Snapshot.

## Lokale Bridge und OpenAI-Tunnel

1. Für lokale MCP-Nutzung genügt das Standardbibliothek-Python, mit dem die Anwendung gestartet wird; ein separates Netzwerkpaket ist nicht erforderlich (Remote-TLS-Zertifikatserstellung nutzt zusätzlich `cryptography`).
2. Die **Tunnel-ID** unter [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels) erstellen/ansehen. Separat unter [Runtime API keys](https://platform.openai.com/settings/organization/api-keys) einen **Runtime API key** erstellen: Restricted mit **Tunnels Read + Use**. Auch dessen Benutzer benötigt diese Rechte für den Tunnel. Dies ist der externe `CONTROL_PLANE_API_KEY`, kein App-Key, Admin-Key oder lokales Shared Secret. Der Key wird nicht automatisch zusammen mit der Tunnel-ID ausgegeben. Tunnel der richtigen Platform-Organisation und dem gewünschten ChatGPT-Workspace zuordnen; siehe [offizielle Anleitung](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) und [Berechtigungen](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/permissions.md).
3. In „Verbindung einstellen“ → **Lokal** nur **Tunnel-ID** und **Runtime API key** eingeben und **„Starten & testen“** wählen. Tunnel-ID: `tunnel_` gefolgt von 32 kleinen Hexzeichen. Keine normale Pfad-, lokale Token- oder Bridge-Start-Eingabe ist nötig. Die App findet einen geeigneten vorhandenen Client und startet ihre getrennten lokalen Prozesse mit automatisch erzeugten Controller-/MCP-Secrets und Loopback-Ports.
4. Fehlt der Client, zeigt die App den Installationsfortschritt. Unter Linux amd64/arm64 installiert sie den offiziellen **v0.0.15 Runtime-Download** aus `openai/tunnel-client` mit fest im Code geprüfter SHA-256, beschränkter ZIP-Verarbeitung und privaten Dateien einschließlich Lizenz/Notice. Es gibt keine automatischen Updates. Unter macOS nutzt sie vorhandenes Homebrew mit `brew install openai/tools/tunnel-client`; fehlt Homebrew, erklärt sie die Voraussetzung. Die von OpenAI nicht notarisierten direkten macOS-ZIPs werden nicht automatisch installiert, Gatekeeper nicht umgangen. Python-Pakete installiert dieser Flow nicht.
5. Die Zustände benennen Client-Suche/Installation, lokalen Start und Prüfung von Runtime API key/Tunnel-Zugriff. **Erst wenn der lokale Client bereit ist und seine Health-Schnittstelle einen tatsächlich erfolgreichen Control-Plane-Poll meldet, werden die Einstellungen dauerhaft geschützt gespeichert.** `READY` allein genügt nicht. 401/403/404, fehlende Rechte, Installation und Zeitüberschreitungen erhalten konkrete Hinweise. Bei Fehlschlag werden die neu gestarteten lokalen Prozesse beendet; vorher gespeicherte Einstellungen bleiben erhalten. Stoppen/„Verbindung beenden“ erscheint erst zustandsabhängig nach erfolgreicher Einrichtung.
6. Im bereiten Zustand „Chatbindung anzeigen“ und bei Bedarf „Chat-Code kopieren“: den lokalen Chat-Code bewusst genau dem gewünschten AI-Chat übergeben. Der Code wird bei jedem MCP-Tool als `conversation` benötigt. Andere Chats erhalten ihn nicht durch Toolabfragen.
7. AI einen einfachen `printf`-Vorschlag zustellen lassen. Vorschlag und anschließend Output jeweils getrennt im Controller prüfen und freigeben. Die AI kann nur diesen freigegebenen Output abholen; erst der im Chat beobachtete Empfang belegt den gesamten Round Trip.

**„Verbindung bereit · Tunnel-Zugriff geprüft“** bedeutet im Local-Flow: lokale Bereitschaft und beim Setup beobachtetes authentifiziertes Polling. Es beweist weder die richtige ChatGPT-App-/Workspace-Zuordnung noch einen ChatGPT-Round-Trip oder kontinuierlich gültige Berechtigungen. Die Oberfläche fordert deshalb ausdrücklich **„ChatGPT-Round-Trip noch manuell prüfen“**. Die zugrunde liegende `/readyz`-Antwort alleine bleibt nur lokale Bereitschaft.

Die Einrichtung bleibt in einem einzigen, größenveränderbaren und vertikal scrollbaren Dialog. Local zeigt den Zwei-Werte-Flow; Remote behält seine Bridge-/Gateway-/Pairing-Details. Es gibt keine Setup-Reiter oder Next/Back-Seiten. Erneutes Öffnen verwendet das bestehende Fenster. Schließen beendet nur das Einstellungsfenster; eine gestartete Einrichtung/Kopplung oder Verbindung läuft weiter. Ergebnisse und Fehler bleiben im Dialog sichtbar.

**Installationsfehler diagnostizieren:** Unter der Fehlermeldung steht jetzt eine kurze Ursache; „Technische Details anzeigen“ öffnet im gleichen Fenster eine schreibgeschützte, vertikal scrollbare Diagnose mit Phase, Subprozess/Komponente, Installationsziel, Exit-Code/Exception und gekürzter/redigierter stdout-/stderr-Ausgabe. Dieselbe Diagnose erscheint auf stderr in der Konsole des Python-Starts. API-Keys/Tokens und credentialartige Zeilen werden vor der Ausgabe entfernt. Der Hinweis beim Schließen ist weiterhin nur eine Lifecycle-Erklärung, keine Fehlerdiagnose.

Auf macOS wird das Ziel über `brew --prefix` ermittelt: normalerweise `/opt/homebrew` (Apple Silicon) oder `/usr/local` (Intel), dort `Cellar/tunnel-client` mit versioniertem libexec. Normale Formula-Installation benötigt nach korrekt eingerichtetem Homebrew keine erhöhten Rechte; die App startet kein Root-Brew und erwartet deshalb keinen Passwortdialog. Ein nicht beschreibbares/falsch zugeordnetes Präfix muss durch den Homebrew-Eigentümer bzw. den vorgesehenen administrativen Provisionierungsweg eingerichtet werden, nicht durch automatische Rechteänderung im Setup. Am Prüftag liefert der offizielle OpenAI-Tap 0.0.14; ein erfolgreicher Install-Exit 0 kann deshalb anschließend als **CLIENT_CHECK / Version zu alt (benötigt ≥0.0.15)** scheitern. Diese Ursache wird jetzt ausdrücklich berichtet. Ein unterstützter aktueller Client/Kanal bleibt Voraussetzung; lokale Bereitschaft ersetzt nicht den benötigten Polling-Nachweis.

Runtime-Key und AI-Transporttoken werden dem Tunnel über private Dateien übergeben, nicht als Klartextargumente oder in dessen Prozessumgebung. Das Terminal und die Bridge erben keine SSH-Agent-Variablen oder allgemeinen Credentials aus der Controllerumgebung. Die gespeicherte Tunnelkonfiguration enthält den Runtime-Key allerdings lokal mit restriktiven Rechten; dies ist kein Betriebssystem-Credential-Store.

Der externe Runtime API key ist nicht mit den internen lokalen Secrets zu verwechseln: die App erzeugt diese zufällig, hält sie intern und übergibt sie per privater Bootstrap-Pipe bzw. `X-AITL-AI-Key`-Dateireferenz. Während des Tests benötigte Secret-Dateien sind privat und temporär; nur die dauerhafte Konfiguration wartet auf erfolgreiche Validierung.

Der Orchestrator-Skill ist erstellt und validiert. Seine Installation im persönlichen Skill-Verzeichnis wurde von der automatischen Freigabeprüfung wegen fehlender ausdrücklicher Zustimmung zur externen Speicherung blockiert. Bis zur Installation kann der MCP-Ablauf direkt verwendet werden. Der Skill ist ausschließlich Bedienhilfe und keine Sicherheitsbarriere.

## Remote-Bridge / Gateway

Auf dem Gateway `python3-cryptography` installieren (Remote-TLS-Zertifikatsgenerierung) und **dieselbe `admin_in_the_loop.py`** dort ablegen. Der GUI-Dialog zeigt einen aus den eingegebenen Remote-Pfaden und der HTTPS-Bridge-Adresse zusammengesetzten, shell-quotierten Erststartbefehl. Beispiel (Platzhalter an das Remote-System anpassen):

```bash
python3 /opt/aitl/admin_in_the_loop.py --bridge \
  --state-dir /home/USER/.local/state/aitl-bridge \
  --bind 0.0.0.0 --port 8443 --tls-name bridge.example --pair
```

Das State-Verzeichnis muss zum ausführenden Benutzer gehören; die Bridge erzeugt es mit Modus `0700` und legt Geheimnisse in Dateien mit `0600` ab. `--pair` öffnet beim Erststart das zweiminütige Fenster. Für einen späteren Start desselben Dienstes `--pair` weglassen. Der HTTP-Port und TLS-Name müssen zur Bridge-Adresse passen; der MCP-Port auf Loopback wird dynamisch zugewiesen und beim Start ausgegeben.

Die lokale Einrichtungskonsole zeigt den vollständigen SHA-256-SPKI-Fingerprint, den Chat-Code, die Ports und einen **zweiminütigen Einmal-Kopplungscode**. Diese Ausgabe gehört zur bewussten Credential-Einrichtung und soll nicht in allgemeine Logs übernommen werden. Nach einem Neustart mit `--pair` wird ein neues Fenster geöffnet.

Im Controller den Bridge-Standort **Remote** wählen: Remote-Startwerte/HTTPS-URL eintragen, den angezeigten Startbefehl am Gateway ausführen und vollständigen SPKI-Fingerprint sowie Kopplungscode aus dessen Konsole übernehmen. Den Fingerprint unabhängig am Gateway vergleichen und dies in der Oberfläche bestätigen; Änderungen an Adresse/Pin setzen diese Bestätigung zurück. Erst „Koppeln und speichern“ beginnt die Verbindung. Der Controller prüft vor dem Senden von HTTP-Credentials Schlüssel-Pin, Zertifikatsgültigkeit, Namen und Zertifikatsprofil. Remote-HTTP wird abgewiesen; Umleitungen und geerbte Proxyeinstellungen werden nicht verwendet.

Die Kopplung wird vor ihrer Bestätigung geschützt gespeichert. Nach einer verlorenen Bestätigungsantwort „Gespeicherte Remote-Verbindung öffnen“ verwenden; dabei werden die geschützten Daten neu geladen und genau derselbe Token bestätigt, ohne einen neuen Claim. Dies funktioniert auch nach Schließen/Wiederöffnen des Dialogs. Die Aktion verlangt passende Adresse und Pin; das Vorhandensein einer Datei allein bedeutet noch keine erfolgreiche Verbindung. Bestätigen einer neuen Kopplung widerruft andere Controller-Tokens für diese Bridge. Ein Gateway betreut im MVP genau eine Chatbindung und einen aktiven Controller.

Der MCP-Port bleibt auch auf dem Gateway bei `127.0.0.1`. Dort läuft der separat gestartete Tunnel-Client, zum Beispiel:

```bash
tunnel-client-runtime run \
  --control-plane.tunnel-id tunnel_IHRE_ID \
  --control-plane.api-key file:/privater/pfad/runtime-key \
  --mcp.server-url http://127.0.0.1:MCP_PORT/mcp \
  --mcp.extra-headers 'X-AITL-AI-Key: file:/privater/pfad/ai-token' \
  --mcp.discovery-extra-headers 'X-AITL-AI-Key: file:/privater/pfad/ai-token' \
  --health.listen-addr 127.0.0.1:0 --log.level error --log.file /dev/null
```

Platzhalter durch die eigene Konfiguration ersetzen. `ai-token` liegt im Bridge-State-Verzeichnis. Key-Dateien müssen privat sein: Verzeichnis `0700`, Dateien `0600`. Beim Remote-Betrieb steuert die Controller-GUI den Tunnel auf dem anderen Rechner nicht; dessen Lifecycle ist Aufgabe des Gateway-Betreibers.

Der Remote-Teil des gleichen Dialogs zeigt diese Gateway-Anleitung statt lokaler Tunnel-Start-/Speicheraktionen. **„Remote-Bridge verbunden · authentifiziert“ ist der erfolgreiche Controller↔Bridge-Aufbau**, nicht der Nachweis einer ChatGPT-/Provider-Verbindung. „Tunnelbetrieb am Gateway“ bezeichnet den Betriebsort, keine beobachtete Gateway-Health. Die echte Provider-Konnektivität bleibt der oben beschriebene manuelle Round Trip; es gibt keine neue Provider-Verifikationsfunktion.

Controller lokal mit derselben Datei widerrufen:

```bash
python3 /opt/aitl/admin_in_the_loop.py --bridge \
  --state-dir /home/USER/.local/state/aitl-bridge --revoke CONTROLLER_ID
```

Zertifikat mit unverändertem SPKI-Schlüssel erneuern, anschließend Bridge neu starten:

```bash
python3 /opt/aitl/admin_in_the_loop.py --bridge \
  --state-dir /home/USER/.local/state/aitl-bridge \
  --tls-name bridge.example --renew-cert
```

Die Zertifikate gelten ein Jahr; der Controller warnt bei weniger als 30 Tagen Restlaufzeit. Ein neuer Schlüssel wird niemals automatisch akzeptiert, sondern verlangt einen erneuten unabhängigen Vergleich und Kopplung.

## Sicherheitsmodell und präzisierte Grenzen

Freigaben sind an Review-ID, exakte UTF-8-Payload, Terminalsession beziehungsweise Bridge-Empfänger, Instanz und Verbindungsgeneration gebunden. Bearbeitung, Richtungswechsel, Verbindungswechsel, Ablauf oder Neustart entwerten alte Freigaben. Netzwerkereignisse stellen ausschließlich Daten bereit. Es gibt keine wiederhergestellten oder vorausgenommenen Freigaben.

Ein Command wird erst nach der lokalen Freigabe und der Prüfung im PTY-Helfer geschrieben. Bei ungewissem Terminaltransfer gibt es **keine automatische Wiederholung**. Output darf nach einer ungewissen HTTP-Zustellung erneut gesendet werden, ausschließlich mit identischem freigegebenem Body, Empfänger und Idempotenzschlüssel, innerhalb des Gültigkeitsfensters. Dies ist keine Freigabe für neue Daten.

Sichtbar ist die sichere Darstellung des tatsächlich übertragenen Textes: echte LF mit Marker, Nicht-ASCII-/Steuerzeichen mit eindeutiger Notation. Darstellungsmarker werden nicht übertragen. Im Review steht ein eingefrorener Snapshot; die UI rekonstruiert keinen Command aus gerenderten Zeichen.

Der Output-Sanitizer verarbeitet UTF-8 und Terminalsequenzen über Chunkgrenzen hinweg. ANSI-/OSC-/DCS-Inhalte werden als Daten behandelt und aus der Auswahlprojektion entfernt; ungültige oder unvollständige Daten werden kenntlich gemacht. Im Linux-xterm-Launcher sind Terminalfunktionen zur Änderung von Clipboard, Fenstern, Fonts, Titeln und weiteren Ressourcen deaktiviert. OSC-52-Clipboard-Manipulation wurde praktisch geprüft. Terminal.app ist nicht entsprechend abgenommen.

Auswahlbereiche sind **Beobachtungsintervalle**, keine beweisbaren Command-Ergebnisse. Der Marker entsteht beim Transfer, nicht beim erkannten Abschluss. SSH, Hintergrundjobs, interaktive Programme und manuelle Eingaben können darin gemischte Ausgaben erzeugen. Ein unbekannter Prompt erfordert eine bewusste zusätzliche Bestätigung; das ist keine sichere Erkennung, dass gerade eine Shell statt einer Passwortabfrage aktiv ist.

Passworteingaben werden nicht mitgeloggt. Der PTY-Helfer vermittelt notwendigerweise Tastatureingaben zum inneren PTY, speichert sie jedoch nicht. Nur beobachteter Output wird gepuffert. Deaktiviertes Echo verhindert normalerweise das Auftauchen von Passwörtern; bei aktiviertem Echo oder bei Ausgaben durch Programme können Secrets trotzdem im Output stehen.

Wesentliche Vertrauensgrenzen:

- **Gleiche Benutzerkennung ist keine starke Prozessisolation.** Der lokale Bridge-Prozess erbt keine administrativen Deskriptoren, kann als Prozess desselben Benutzers aber grundsätzlich auf dessen Dateien zugreifen. Für sensible Systeme Bridge und Tunnel unter eigener Identität oder in separatem Qube/Gateway betreiben.
- Der Chat-Code ist eine **menschlich ausgewählte Bearer-Bindung**, keine von OpenAI kryptografisch bestätigte Chat-ID. Wer sowohl App-Zugriff als auch den Code besitzt, kann die bereits für diese Bridge freigegebenen Daten lesen. Nie an weitere Chats oder aus untrusted Output übernehmen.
- Menschliche Zustimmung verhindert keine schlechten Entscheidungen oder Prompt Injection. Shellcommands können selbst Daten lesen, löschen oder nach außen senden. Es gibt keine deterministische sichere Einstufung aller Shellsemantik und keine fertige Secret-Erkennung/Redaction.
- Bridge-Zustände und freigegebener Output bleiben höchstens **24 Stunden** abrufbar. Dies ist keine Löschgarantie für Datenträger, Backups oder einen AI-Anbieter.
- Diese Version ist ein getesteter MVP, keine Abnahme für kritische Infrastruktur. Die konkrete Betriebstrennung und Befehlsprüfung bleiben entscheidend.

## Limits, Speicherung und Fehlerverhalten

Commands maximal 4096 UTF-8-Bytes und 50 Zeilen. Output maximal 64 KiB; zusätzlich darf seine JSON-Hülle 128 KiB nicht überschreiten. Zu große Inhalte werden abgewiesen statt heimlich gekürzt. Rohoutput-Ring und bereinigte Projektion sind begrenzt; verlorene Präfixe bleiben kenntlich. Eine Vollterminalemulation findet nicht statt.

HTTP-/MCP-Nachrichten haben feste Schemata, Body-/Header-/Zeitlimits, Rollenprüfung, Rate Limits und begrenzte Parallelität. Doppelte JSON-Schlüssel, NaN, unbekannte Felder, hostile Origins/Hosts und ungültige Cursor werden gesperrt. Es gibt kein `eval`, keine dynamischen Plugins und keine Ausführung von Metadaten. Fehler enthalten feste Gründe statt Payloads oder Tracebacks.

Controller-Einstellungen:

- Debian/Linux: `~/.config/admin-in-the-loop/`
- macOS: `~/Library/Application Support/Admin in the Loop/`

`preferences.json` enthält Return und Blockgröße. `connection.json` enthält eine Remote-Verbindung und/oder Tunnelkonfiguration. Eigentümer und Rechte werden geprüft; Verzeichnis `0700`, Dateien `0600`, atomare Speicherung, Symlinks abgewiesen. Fehlerhafte Präferenzen werden nicht still mit Return an zurückgesetzt. Die Bridge führt ihre begrenzte Transportdatenbank in einem getrennten State-Verzeichnis; Tokenwerte liegen dort gehasht in SQLite, benötigte lokale Runtime-Secrets zusätzlich in privaten Dateien.

## Headless-Programmschnittstelle

Alle GUI-Aktionen führen in denselben Kern beziehungsweise lokale Launcher. `AppCore()` ohne explizite Stores bleibt flüchtig. Lokale Tests können den gesamten Ablauf und den aktuellen Review ohne GUI steuern. Aktionen und `pump()` gehören auf denselben Ereignisthread; Netzwerkthreads dürfen nicht freigeben.

| Aktion | Schnittstelle |
|---|---|
| Terminal vorbereiten/starten | `start_local()`, `helper_argv()`, `helper_command()`, `launch_terminal()` |
| Zustände und exakten Review lesen | `pump()`, `snapshot()`, `bridge_snapshot()`, `review_fragments()` |
| Vorschlag bereitstellen und prüfen | `add_proposal()`, `review_pending_proposal()` |
| Review erstellen, bearbeiten, verwerfen | `review_command()`, `edit_review()`, `reject()` |
| Return, Blockgröße, Ausgabeauswahl | `set_final_return()`, `set_preferences()`, `set_output_mode()`, `select_current_output()` |
| Freigabe / identischen Output erneut zustellen | `approve()`, `retry_output()` |
| Bridge verbinden/trennen | `connect_local_bridge()`, `connect_profile()`, `connect_saved_bridge()`, `reconnect_bridge()`, `disconnect_bridge()` |
| Remote koppeln | `pair_remote()`; GUI nutzt `begin_pair_remote()` |
| Tunnel konfigurieren/starten/stoppen | `configure_tunnel()`, `start_tunnel()`, `stop_tunnel()` |
| Local-Zwei-Werte-Einrichtung | `begin_local_setup(tunnel_id, api_key)`; `pump()` übernimmt Vorbereitung/Start und prüft vor dauerhafter Speicherung |

Die lokale Test-API ist privilegierter Anwendungscode. Sie darf nicht als AI-Tool oder freigabeferner HTTP-Endpunkt veröffentlicht werden.

## HTTP und MCP

Controllerrolle: `GET /v1/status`, `GET /v1/inbox`, `POST /v1/approved-output`, `PUT /v1/preferences`, Kopplung über `POST /v1/pair/claim` und `/v1/pair/confirm`. Authentisierte Inbox mit begrenztem Long-Polling, Backoff und opaken Cursorwerten. Der lokale Controller zeigt immer einen Vorschlag; weitere Bridge-Vorschläge ersetzen nie einen laufenden Review.

AI-Rolle ausschließlich über `POST /mcp`:

| Tool | Argumente zusätzlich zu `conversation` | Ergebnis |
|---|---|---|
| `submit_proposal` | `request_id`, `text` | Empfang/ID; keine Ausführung |
| `get_submission_status` | `submission_id` | Empfangsstatus und Vorhandensein freigegebenen Outputs |
| `get_approved_output` | optional `submission_id`, `cursor` | Ausschließlich bereits freigegebener Output |
| `get_preferences` | keine | SINGLE/SMALL/MEDIUM/LARGE |

Vier Tools, keine Resources, Shell-, Datei-, Tastatur- oder Terminal-Lesetools. Ein MCP-Tools-Teilumfang mit Legacy-Initialisierung und neuer Discovery ist implementiert; kein allgemeines Agentenframework. `ACCEPTED` bedeutet **angenommen, nicht ausgeführt**. Leerer Output beweist keinen Fehler oder Erfolg.

Output wird exakt einmal als eigener Text-Contentblock übertragen. Der erste Block und `structuredContent` enthalten Metadaten; `text_content_index` benennt den jeweiligen Textblock. Dadurch bleibt Output auch bei Hosts verfügbar, die `structuredContent` ignorieren, ohne den Nutztext doppelt zu übertragen.

## Tests und historischer Abnahmestand

```bash
python3 admin_in_the_loop.py --self-test
python3 tests/test_connected_mvp.py
python3 tests/test_local_setup.py
```

Für die zusätzlichen zwei Tests mit dem echten offiziellen Tunnelbinary:

```bash
AITL_TEST_TUNNEL_BINARY=/voller/pfad/tunnel-client-runtime python3 tests/test_connected_mvp.py
AITL_TEST_TUNNEL_BINARY=/voller/pfad/tunnel-client-runtime python3 tests/test_local_setup.py
```

GUI-Smoke-Test unter einem virtuellen Linux-Display, weiterhin mit Python-`unittest`:

```bash
sudo apt install xvfb xauth xterm python3-tk
xvfb-run -a -s '-screen 0 1920x1080x24' python3 tests/gui_smoke_test.py
```

**86 Tests bestanden:** 49 bestehende, 36 Netzwerk-/Integrationstests, ein GUI-Test. Die Tunneltests verwenden das reale OpenAI-Binary und eine lokale simulierte Kontrollplattform; keine OpenAI-Credentials. Prüfumgebung Ubuntu 24.04, Linux x86_64, Python 3.12.14, aiohttp 3.13.5, cryptography 46.0.0, Tk 9.0.4, xterm 390. Protokolle liegen bei; Details in `MVP_0.8_TEST_REPORT.md`.

Die im Testbericht und in der damaligen Statusdatei genannten Lücken (echte ChatGPT-/OpenAI-Verbindung, native macOS-/Terminal.app- und Debian-Abnahme, Qubes-/Gateway-Isolation sowie reale SSH-/sudo-/TUI-Proben) beschreiben den dokumentierten historischen Abnahmestand, keine aktuelle To-do-Liste. Aktuelle offene Arbeit, Prioritäten und Ticketzustände werden ausschließlich im Taskboard gepflegt; die Statusdatei und das Vorschlagsdokument dienen nur als historische Implementierungs-/Evidenz- beziehungsweise Migrationsreferenz.
