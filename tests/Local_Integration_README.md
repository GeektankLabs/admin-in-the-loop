# Admin in the Loop – v0.9.0 (persönliche Testversion)

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
3. Rechts „← Prüfen“, den Text in der Mitte prüfen, anschließend „← Freigeben ins Terminal“. Eine geprüfte explizite Freigabe genügt auch bei unklarem Eingabezustand; die Statuszeile zeigt dann den ehrlichen Hinweis „Eingabezustand unklar“ (ggf. Passwort-/Fortsetzungs-Kennzeichnung), blockiert aber nicht.
4. Nach dem Transfer links Ausgabe wählen und „Prüfen →“. Eine andere Auswahl lässt sich erneut in den Review übernehmen.
5. Erst „Freigeben an AI →“ gibt genau diesen Snapshot frei. Ohne Bridge wird lediglich die lokale Testfreigabe vermerkt; es erfolgt keine Anbieterübertragung.

Textbearbeitung über das Kontextmenü des Reviews oder **Ctrl+E**, auf macOS zusätzlich **Cmd+E**. Die Übernahme aus dem Editor erzeugt eine neue Review-ID. Die Hauptfläche bleibt ein fester, schreibgeschützter Snapshot.

### Exakte Bytes, Normalisierung und lokale Zwischenablage

- **„Bytes / Codepoints…“** zeigt ausschließlich den finalen eingefrorenen UTF-8-Snapshot: Byteoffsets, Hexbytes, Codepoints und sichere JSON-Zeichennotation, mit Review-ID/Revision/Richtung/finalem Return. Ein neuer Review, eine Bearbeitung, ein Return-/Session-/Bindingwechsel oder Ablauf kennzeichnet die alte Ansicht als **nicht mehr aktuell**; deren Vollsnapshot-Copy wird gesperrt. Die Ansicht führt nichts aus und erteilt keine Freigabe.
- Die Review-Kopfzeile zeigt transiente Zählwerte für **CRLF→LF**, **CR→LF**, entfernte Schluss-LF und das aktuelle finale Return. Es wird kein ursprünglicher Commandtext archiviert. Bestehende Normalisierung, Unicode und finale Payloadbytes bleiben unverändert; neue Bearbeitung erzeugt neue Hinweise für die neue Revision. Output wird nicht als Command normalisiert.
- Ausgewählten Reviewtext mit **Ctrl+C** (macOS **Cmd+C**) oder **„Auswahl kopieren“** im Kontextmenü lokal kopieren. Bei ganzen Zeichen kopiert die App den zugehörigen Originaltext statt synthetischer `↵`-/Codepointnotation. Softwrap ist kein Datennewline. Eine Auswahl mitten in einer Zeichennotation wird mit Hinweis abgelehnt und lässt die Zwischenablage unverändert; ganze Zeichen wählen oder in der Detailansicht bewusst **„Finalen Snapshot exakt kopieren“**. Details-/Diagnosetabellen kopieren ihre sichtbare Notation.
- Editierbare Textfelder/Entries behalten native Paste (**Ctrl+V**, macOS **Cmd+V**); readonly Review-/Output-/Detailflächen bleiben schreibgeschützt. Chatcode-Copy bleibt bewusst bedient und an gültigen/frischen Status gebunden. Keine automatische Freigabe, Submission, Outputweitergabe oder Secretprotokollierung durch Copy/Paste.
- Das **separate Systemterminal** behält seine eigenen Clipboardwege: typischerweise **Ctrl+Shift+C/V** im Debian-xterm, **Cmd+C/V** in Terminal.app oder dessen Kontextmenü. **Ctrl+C bleibt Terminal-SIGINT**; die Tk-Copybindungen gelten nur für Tk-Textwidgets. Terminalcopy ist die native sichtbare Auswahl, keine Roh-PTY-Byteaufnahme. Vorhandene OS-Clipboardhistorie ist eine lokale Betriebssystemgrenze.

Native Acceptance separat mit ausschließlich synthetischen Token-/Unicode-/Multiline-Fixtures: reales Debian/PBS-Clipboard, Terminal-SIGINT und macOS-Clipboard/Terminal.app. Xvfb/Tk/xterm-Fixtures sind lokale Regression, **kein nativer Debian-PBS-/Mac-PASS**. Hosted-/Tunnel-/SSH-Zielnachweise bleiben eigenständige offene Evidenzgrenzen.

## Lokale Bridge und OpenAI-Tunnel

1. Für lokale MCP-Nutzung genügt das Standardbibliothek-Python, mit dem die Anwendung gestartet wird; ein separates Netzwerkpaket ist nicht erforderlich (Remote-TLS-Zertifikatserstellung nutzt zusätzlich `cryptography`).
2. Die **Tunnel-ID** unter [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels) erstellen/ansehen. Separat unter [Runtime API keys](https://platform.openai.com/settings/organization/api-keys) einen **Runtime API key** erstellen: Restricted mit **Tunnels Read + Use**. Auch dessen Benutzer benötigt diese Rechte für den Tunnel. Dies ist der externe `CONTROL_PLANE_API_KEY`, kein App-Key, Admin-Key oder lokales Shared Secret. Der Key wird nicht automatisch zusammen mit der Tunnel-ID ausgegeben. Tunnel der richtigen Platform-Organisation und dem gewünschten ChatGPT-Workspace zuordnen; siehe [offizielle Anleitung](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) und [Berechtigungen](https://github.com/openai/tunnel-client/blob/v0.0.15/docs/permissions.md).
3. In „Verbindung einstellen“ → **Lokal** nur **Tunnel-ID** und **Runtime API key** eingeben und **„Starten & testen“** wählen. Tunnel-ID: `tunnel_` gefolgt von 32 kleinen Hexzeichen. Keine normale Pfad-, lokale Token- oder Bridge-Start-Eingabe ist nötig. Die App findet einen geeigneten vorhandenen Client und startet ihre getrennten lokalen Prozesse mit automatisch erzeugten Controller-/MCP-Secrets und Loopback-Ports.
4. Fehlt der Client, zeigt die App den Installationsfortschritt. Unter Linux amd64/arm64 installiert sie den offiziellen **v0.0.15 Runtime-Download** aus `openai/tunnel-client` mit fest im Code geprüfter SHA-256, beschränkter ZIP-Verarbeitung und privaten Dateien einschließlich Lizenz/Notice. Es gibt keine automatischen Updates. Unter macOS nutzt sie vorhandenes Homebrew mit `brew install openai/tools/tunnel-client`; fehlt Homebrew, erklärt sie die Voraussetzung. Die von OpenAI nicht notarisierten direkten macOS-ZIPs werden nicht automatisch installiert, Gatekeeper nicht umgangen. Python-Pakete installiert dieser Flow nicht.
5. Die Zustände benennen Client-Suche/Installation, lokalen Start und Prüfung von Runtime API key/Tunnel-Zugriff. **Erst wenn der lokale Client bereit ist und seine Health-Schnittstelle einen tatsächlich erfolgreichen Control-Plane-Poll meldet, werden die Einstellungen dauerhaft geschützt gespeichert.** `READY` allein genügt nicht. 401/403/404, fehlende Rechte, Installation und Zeitüberschreitungen erhalten konkrete Hinweise. Bei Fehlschlag werden die neu gestarteten lokalen Prozesse beendet; vorher gespeicherte Einstellungen bleiben erhalten. Stoppen/„Verbindung beenden“ erscheint erst zustandsabhängig nach erfolgreicher Einrichtung.
6. Der aktuelle **8-stellige Chat-Code** wird nach authentisierter Bridge-Verbindung dauerhaft rechts unter AI/MCP angezeigt; optional dort oder im Verbindungsdialog kopieren und bewusst nur dem gewünschten Chat mitteilen. Er wird in Vierergruppen wie `7K4M-9R2X` dargestellt und bleibt **24 Stunden** gültig. MCP-Tools sowie Event-Subscribe/Refresh/Unsubscribe verwenden ihn weiter im Argument `conversation`; die interne Conversation-ID bleibt davon getrennt. Andere Chats erhalten den Code nicht durch Toolabfragen. Nach Ablauf oder bei Bedarf im vorhandenen Verbindungsdialog „Neuen Chat-Code erzeugen“ wählen und den neuen Code im ausgewählten Chat bekanntgeben; alte Event-Abos müssen neu autorisiert werden. Tippfehler werden toleriert; nach mehreren Fehlversuchen folgen progressive Wartezeiten und eine temporäre Sperre. Bei Backoff/Sperre Statusanzeige beachten statt wiederholt zu probieren.
7. AI einen einfachen `printf`-Vorschlag zustellen lassen. Vorschlag und anschließend Output jeweils getrennt im Controller prüfen und freigeben. Die AI kann nur diesen freigegebenen Output abholen; erst der im Chat beobachtete Empfang belegt den gesamten Round Trip.

**„Verbindung bereit · Tunnel-Zugriff geprüft“** erscheint ausschließlich im belegten Local-Setup: lokale Bereitschaft und beim Setup beobachtetes authentifiziertes Polling. Es beweist weder die richtige ChatGPT-App-/Workspace-Zuordnung noch einen ChatGPT-Round-Trip oder kontinuierlich gültige Berechtigungen. Der permanente Round-Trip-Zusatz entfällt im Normalstatus; die gezielte Prüfanleitung bleibt im Verbindungsdialog verfügbar. Remote zeigt nur **„Remote-Bridge verbunden · authentifiziert / Tunnelbetrieb am Gateway“**, keinen unbeobachteten Tunnelcheck. Die zugrunde liegende `/readyz`-Antwort alleine bleibt nur lokale Bereitschaft. Im normalen gültigen Chatcodezustand entfallen permanente Frist-/Weitergabehinweise; Ablauf, stale Status, Sperre, Backoff und Disconnect bleiben konkret sichtbar. Rotation bleibt manuell.

Die Einrichtung bleibt in einem einzigen, größenveränderbaren und vertikal scrollbaren Dialog. Local zeigt den Zwei-Werte-Flow; Remote behält seine Bridge-/Gateway-/Pairing-Details. Seit W4 gibt es drei eindeutige Verbindungsarten: „Auf diesem System / in diesem Qube", „Anderer Qube — über Qrexec" (nur bei erkanntem Qubes) und „Remote — über HTTPS" (freies Ziel, auch auf Qubes immer verfügbar). Es gibt keine Setup-Reiter oder Next/Back-Seiten. Erneutes Öffnen verwendet das bestehende Fenster. Schließen beendet nur das Einstellungsfenster; eine gestartete Einrichtung/Kopplung oder Verbindung läuft weiter. Ergebnisse und Fehler bleiben im Dialog sichtbar.

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

Auswahlbereiche sind **Beobachtungsintervalle**, keine beweisbare Command-Ergebnisse. Der Marker entsteht beim Transfer, nicht beim erkannten Abschluss. SSH, Hintergrundjobs, interaktive Programme und manuelle Eingaben können darin gemischte Ausgaben erzeugen. Die Prompt-/Bereitschaftserkennung ist eine **großzügige, ehrliche Klassifikation** des sichtbaren Eingabe-Endes (nach passiver Normalisierung von CSI-/OSC-/CR-/Backspace-Resten): Sie liefert die Klassen Prompt, Passwort-/Passphrase-Abfrage, Fortsetzung (`>`/PS2-artig) oder unklar. Sie **beweist keine Ausführbarkeit** und blockiert die explizite menschengereviewte Freigabe nie – ein zweiter Klick existiert bewusst nicht mehr; die einzige Sperre vor dem Senden bleibt die bewusste Freigabe selbst plus die getrennten Zustellungs-Interlocks (uncertain/open_return). Ein als Prompt klassifiziertes Ende kann trotzdem ein Programm-Prompt sein (dokumentierte False-Positive-Klasse).

Passworteingaben werden nicht mitgeloggt. Der PTY-Helfer vermittelt notwendigerweise Tastatureingaben zum inneren PTY, speichert sie jedoch nicht. Nur beobachteter Output wird gepuffert. Deaktiviertes Echo verhindert normalerweise das Auftauchen von Passwörtern; bei aktiviertem Echo oder bei Ausgaben durch Programme können Secrets trotzdem im Output stehen.

Wesentliche Vertrauensgrenzen:

- **Gleiche Benutzerkennung ist keine starke Prozessisolation.** Der lokale Bridge-Prozess erbt keine administrativen Deskriptoren, kann als Prozess desselben Benutzers aber grundsätzlich auf dessen Dateien zugreifen. Für sensible Systeme Bridge und Tunnel unter eigener Identität oder in separatem Qube/Gateway betreiben.
- Der kurzlebige Chat-Code ist eine **menschlich ausgewählte Bearer-Bindung**, keine von OpenAI kryptografisch bestätigte Chat-ID. Er ist getrennt von der stabilen internen Conversation-ID, gilt 24 Stunden und kann ausschließlich über den authentisierten Controller erneuert werden. Server-seitiges progressives Backoff und temporäre Principal-/Binding-Sperren begrenzen Online-Raten; ein Angreifer mit gültigem AI-Transportkey kann eine temporäre Sperre auslösen. Wer sowohl App-Zugriff als auch den aktuellen Code besitzt, kann die bereits für diese Bridge freigegebenen Daten lesen. Nie an weitere Chats oder aus untrusted Output übernehmen.
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
| Vorschlag bereitstellen und prüfen | `add_proposal()`, `review_pending_proposal()` (bridge-gebunden asynchron: `TAKE_PENDING` bis Bridge-ACK) |
| Übernahme-Pending verwerfen | `discard_pending_take()` (stellt den Vorzustand wieder her, entwertet die Nonce; Bridge-`TAKEN` bleibt) |
| Review erstellen, bearbeiten, verwerfen | `review_command()`, `edit_review()`, `reject()` |
| Return, Blockgröße, Ausgabeauswahl | `set_final_return()`, `set_preferences()`, `set_output_mode()`, `select_current_output()` |
| Offene Rückgabe (W2/M5) | `take_return_review()`, `close_open_return()`, `note_output_binding()`, `resolve_stage_conflict()`, `resume_staged_proposal()`, `stage_conflict_info()` |
| Freigabe / identischen Output erneut zustellen | `approve()`, `retry_output()` |
| Bridge verbinden/trennen | `connect_local_bridge()`, `connect_profile()`, `connect_saved_bridge()`, `reconnect_bridge()`, `disconnect_bridge()` |
| Remote koppeln | `pair_remote()`; GUI nutzt `begin_pair_remote()` |
| Tunnel konfigurieren/starten/stoppen | `configure_tunnel()`, `start_tunnel()`, `stop_tunnel()` |
| Local-Zwei-Werte-Einrichtung | `begin_local_setup(tunnel_id, api_key)`; `pump()` übernimmt Vorbereitung/Start und prüft vor dauerhafter Speicherung |

Die lokale Test-API ist privilegierter Anwendungscode. Sie darf nicht als AI-Tool oder freigabeferner HTTP-Endpunkt veröffentlicht werden.

## HTTP und MCP

Controllerrolle: `GET /v1/status`, `GET /v1/inbox`, `POST /v1/approved-output`, `PUT /v1/preferences`, `POST /v1/proposal/take` und `POST /v1/proposal/observation` (W2/M5), Kopplung über `POST /v1/pair/claim` und `/v1/pair/confirm`. Authentisierte Inbox mit begrenztem Long-Polling, Backoff und opaken Cursorwerten; ausgeliefert werden nur noch wartende (`WAITING`) Vorschläge. `POST /v1/proposal/take` überführt atomar `WAITING→TAKEN` (Nonce-idempotent, `expected_instance`-gebunden); `POST /v1/proposal/observation` schreibt Controller-Beobachtungen `TERMINAL_SENT`/`OUTPUT_REVIEWED` first-write-wins auf Serverclock-Basis, nur bei `TAKEN`. Feste Fehlerhüllen `{"error": code}`; gutartige Rennen (`proposal_withdrawn`, `proposal_taken`, `proposal_unknown`, `submission_expired`, `submission_unavailable`, `proposal_not_taken`) sind für den Worker nicht fatal, echte Instanz-/Bindungsfehler bleiben fail-closed. Der lokale Controller zeigt immer einen Vorschlag; weitere Bridge-Vorschläge ersetzen nie einen laufenden Review.

AI-Rolle ausschließlich über `POST /mcp`:

| Tool | Argumente zusätzlich zu `conversation` | Ergebnis |
|---|---|---|
| `submit_proposal` | `request_id`, `text` | Empfang/ID; keine Ausführung |
| `get_submission_status` | `submission_id` | Lifecycle-Fakten: `ACCEPTED`/`TAKEN`/`WITHDRAWN`/`UNKNOWN`, Zeitstempel, `output_shared_ever`, Verfügbarkeit freigegebenen Outputs; keine Rohtexte, keine IDs anderer Vorschläge |
| `get_approved_output` | optional `submission_id`, `cursor` | Ausschließlich bereits freigegebener Output |
| `get_preferences` | keine | SINGLE/SMALL/MEDIUM/LARGE |
| `withdraw_proposal` | `submission_id` | Immer nur `RECORDED` – kein Erfolgsbeweis; maßgeblich ist der folgende `get_submission_status`-Readback |

Fünf Tools, keine Resources, Shell-, Datei-, Tastatur- oder Terminal-Lesetools. Ein MCP-Tools-Teilumfang mit Legacy-Initialisierung und neuer Discovery ist implementiert; kein allgemeines Agentenframework. `ACCEPTED` bedeutet **angenommen, nicht ausgeführt**; `TAKEN` bedeutet **in menschliche Review übernommen, nicht gesendet oder ausgeführt**. `withdraw_proposal` zieht ausschließlich eigene, noch nicht übernommene (`WAITING`) Vorschläge zurück; bereits übernommene bleiben `TAKEN` und werden nur als „Rücknahme angefragt“ vermerkt. Leerer Output beweist keinen Fehler oder Erfolg.

Output wird exakt einmal als eigener Text-Contentblock übertragen. Der erste Block und `structuredContent` enthalten Metadaten; `text_content_index` benennt den jeweiligen Textblock. Dadurch bleibt Output auch bei Hosts verfügbar, die `structuredContent` ignorieren, ohne den Nutztext doppelt zu übertragen.

## Vorschlags-Lifecycle, Review-Stage und eine offene Rückgabe (W2/M5)

Ein bridge-gebundener Vorschlag wird **asynchron** übernommen: Klick „← Prüfen“ erzeugt `TAKE_PENDING` („Übernahme wird bestätigt…“), erst die Bridge-Bestätigung konstruiert den lokalen Review. Take und AI-Rücknahme werden auf der Bridge atomar entschieden – genau eine Seite gewinnt. Verwerfen während `TAKE_PENDING` stellt den vorherigen Zustand exakt wieder her und entwertet die lokale Nonce; ein später ACK wird ignoriert, die Bridge-Zeile bleibt `TAKEN` (kein Zurückdrehen, ehrlicher verwaister Zustand bis zum 24-h-Ablauf).

Die Review-Stage enthält **höchstens einen** Inhalt: entweder einen neuen Kommandovorschlag oder die Ausgabeprüfung des vorherigen Kommandos. Daneben gilt: **höchstens eine offene Rückgabe** bereits übergebener Kommandos, geführt über einen flüchtigen lokalen Datensatz (`open_return`), der bei bestätigter Terminal-Übergabe entsteht und nur durch erfolgreichen Output-Versand oder die ausdrückliche Aktion „ohne Rückgabe abschließen“ endet. Ein neuer Kommandovorschlag darf bei freier Stage auch dann geprüft werden, wenn noch eine Rückgabe offen ist – **tatsächlich ans Terminal gesendet** wird er erst, wenn die offene Rückgabe abgeschlossen ist, er selbst explizit freigegeben wurde und Terminalbereitschaft besteht. `TRANSFERRED` bedeutet weiterhin nur Byte-Übergabe, nie Ausführungsende; unklare Übergaben (`TRANSFER_UNCERTAIN`) sperren neue Sendevorgänge bis zur menschlichen Klärung und werden nie automatisch wiederholt. Eine „belegte Nichtzustellung“ gibt es nicht: fehlgeschlagene Schreibversuche bleiben ungewiss.

Statt stiller Überschreibung öffnen konfliktauslösende Aktionen einen richtungsbezogenen Dialog mit neutralem, voreingestelltem „Abbrechen“:

1. Terminal-Senden bei offener Rückgabe: „Ohne Rückgabe fortfahren und senden“ (Verzicht **nur** auf die Rückgabe; der Versand wird anschließend normal geprüft – schlägt er fehl, bleibt der Verzicht bestehen und wird nicht als Erfolg dargestellt), „Neuen Vorschlag verwerfen“ oder „Abbrechen“. Der Verzicht ist derselbe fachliche Abschluss wie das Verwerfen der Ausgabe im Review-Bereich – es gibt keinen zusätzlichen Abschlusszustand.
2. Ausgabeprüfung bei belegtem Review-Bereich: „Vorschlag verwerfen und Ausgabe prüfen“ oder „Abbrechen“. Der Vorschlag wird dabei **zurückgelegt** und kann ohne erneute Bridge-Übernahme verlustfrei wiederhergestellt werden; das endgültige Verwerfen ist eine getrennte, bewusste Aktion.
3. Vorschlagsprüfung bei aktiver Ausgabeprüfung: „Neuen Vorschlag prüfen“ (verlässt die Ausgabeprüfung **ohne** Verzicht – die Ausgabe bleibt für eine spätere Rückgabe erhalten) oder „Abbrechen“.
4. Terminal-Senden nach ungewisser vorheriger Übertragung: „Die Übertragung des vorherigen Befehls ist ungewiss. Trotzdem den neuen Befehl senden und auf die vorherige Rückgabe verzichten?“ mit „Trotzdem senden und verzichten“ / „Abbrechen“. Die Ungewissheit wird als stage-unabhängiger Fakt geführt, bleibt über Stagewechsel sichtbar („Übertragung des vorherigen Befehls ungewiss“) und wird **nie** nachträglich als erfolgreiche Übertragung dargestellt; die bewusste Bestätigung ist die einzige Entsperrung – kein automatisches Entsperren, kein Retry des alten Befehls.

Headless/CLI bilden dieselben Aktionen explizit ab: `resolve_stage_conflict(...)` mit den fünf Entscheidungen, `confirm_uncertain_send()`, `stage_conflict_info()`/`stage_conflict` sowie `transfer_uncertain_conflict` als feste Konfliktergebnisse, `take_return_review()`, `close_open_return()`, `resume_staged_proposal()`, `discard_pending_take()` und `note_output_binding()`. Die Observation `OUTPUT_REVIEWED` entsteht ausschließlich aus einer **bewussten** Bindungswahl in der Output-Zuordnung, nie aus dem automatischen Standardvorschlag und nie aus `approve()` allein; Freigabe-/Payloadbytes bleiben davon unberührt.

**Bridge-Neustart = neuer Arbeitszyklus:** Ein tatsächlicher (Neu-)Start des Bridge-Dienstes – auch nach Crash, die Initialisierung ist transaktionsatomar und crashsicher – reanimiert keine alten Zykluszustände: wartende/übernommene Vorschläge des vorherigen Laufs werden dauerhaft nicht-ausführbar (`UNKNOWN`, Zeitstempelfakten und `WITHDRAWN`-Abschlüsse bleiben als Historie lesbar), Liefercursor werden verworfen. Dauerhafte Daten bleiben erhalten: Chat-Code/Bindung, Tokens, freigegebene Outputs (retention-begrenzt), Subscriptions (bindungsgenerations-begrenzt) und Einstellungen. Ein Bridge-Neustart ist **kein** Ausführungsrollback und **kein** Übertragungsnachweis. Utility-Öffnungen (`--revoke`, `--renew-cert`) sowie Controller-Reconnect/UI-Neustart sind kein Zyklusreset. Nach Controller-Neustart sind Stage, Pending-Übernahmen, Rücklage, offene Rückgabe und der Ungewissheitsfakt flüchtig verloren (fail-closed, keine Phantomzustände); die Bridge-Fakten (`TAKEN`, `terminal_sent_at`, `output_shared_ever`, Rücknahme-Vermerke) bleiben dort maßgeblich und retention-begrenzt (24 h) erhalten.

## Eingabebereitschaft und gesendete Snapshots (W3/M2+M3)

**Bereitschaftsklassifikation (M2):** `classify_input_tail()` klassifiziert das sichtbare Ende des Terminal-Outputs großzügig und passiv – nach Entfernen üblicher Escape-Reste (CSI, OSC mit BEL/ST oder abgebrochen partiell, Zwei-Byte-Fe-Sequenzen) sowie Auflösen von CR-Redraws und Backspaces – in `PROMPT` (gewöhnliche `$`/`#`/`%`-Enden, breite Shell-/Custom-Formen), `PASSWORD` (Schlüsselwort + `:`-Ende), `CONTINUATION` (`>`-Ende, PS2-/fish-artig) oder `UNKNOWN`. Die Klasse ist ein **ehrlicher Hinweis, keine Sperre**: Bei unklarem Ende zeigt die Statuszeile „Eingabezustand unklar“ (bzw. die Passwort-/Fortsetzungs-Kennzeichnung), die normale explizite Freigabe sendet dennoch sofort – es gibt keinen promptspezifischen Zweitklick mehr. Prompt-Ungewissheit wird nicht mit Zustellungs-Ungewissheit vermischt: Die W2-Interlocks (`TRANSFER_UNCERTAIN`, offene Rückgabe) bleiben unverändert blockierend. Blockreview, Richtungs-/Session-/Revisions-/Binding-Prüfungen, stale/restart-Invalidierung und Command-Risk-Policy sind unverändert; die Output-Capture-Offsets bleiben unabhängig.

**Gesendete Snapshots (M3):** Nach bestätigter Übergabe bleibt der exakte Review-Snapshot sichtbar und erhält eine dezente Sent-Optik (grauer/grüner `sent`-Tag über dem Text; Risk-Farben, `↵`-/Steuerzeichen-Markierungen und Auswahl bleiben darüber priorisiert und lesbar). Richtung zeigt „AI → Terminal · gesendet“ bzw. „Terminal → AI · gesendet“, die Statuszeile „Gesendet ins Terminal · Bytes übertragen“, „Übertragung ungewiss · keine Wiederholung“ oder „Output lokal freigegeben · keine AI-Übertragung“/„Output geteilt · Bridge bestätigt“. Bereits **real analysierte** Risk-Ergebnisse (Zustand `RESULT`, Farben/geprüfter Zustand) bleiben über `CHECKING→TRANSFERRED/TRANSFER_UNCERTAIN` exakt snapshot-/revisionsgebunden erhalten – ohne Reanalyse und ohne Pinfreigabe; jede Revision-/Binding-/Payload-Änderung, Ablauf oder Richtungs-/Sessionwechsel entwertet sie stattdessen. `UNAVAILABLE`/`UNKNOWN` tragen keine Farben und verwerfen weiterhin wie zuvor. Kopieren bleibt im Sent-Zustand exakt (Quelltext, keine Darstellungsmarker), Scrollen und Kontrast bleiben erhalten; ein neuer Review entfernt die Sent-Optik vollständig.

## Bridge-Einrichtung: Defaults, Pairing und Qubes/Qrexec (W4/M9)

**Einfacher Start und persistente Defaults.** `python3 admin_in_the_loop.py` startet wie bisher Main/GUI; `python3 admin_in_the_loop.py --bridge` startet ausschließlich die headless Bridge. Interpreter, eigener Skriptpfad und Home werden automatisch erkannt; State, Konfiguration und TLS liegen dauerhaft privat unter `~/.local/state/aitl-bridge` (0700/0600) – ein Skript-Update z. B. in Downloads verliert sie nicht. `--state-dir` überschreibt mit klarer Auflösung von `~`, relativen Pfaden und Leerzeichen; feldbezogene Fehlermeldungen statt Pauschalvalidierung. Bestehende service-/noninteractive-Starts (Bootstrap-/Ready-Pipes, explizite Flags) bleiben unverändert.

**Defaults ohne Rückfrage.** Standalone startet die Bridge mit Bind `0.0.0.0`, Port `8443` – es gibt keine Bind-Rückfrage; `--bind`/`--port` sind optionale Abweichungen. TLS wird automatisch mit SANs für `localhost`, `127.0.0.1` und erkannte lokale Interface-IPs erzeugt (best-effort, ohne Netzwerk-Probe); zusätzliche Domains/IPs ergänzt der bestehende konsistente Parameter `--tls-name` (kein zweiter Alias). Bind-Adresse und nutzbare Zieladressen werden klar getrennt angezeigt: `0.0.0.0` erscheint nie als URL. Zertifikat und Vertrauen bleiben persistent; `--renew-cert` erhält den SPKI-Fingerprint.

**Erstkopplung.** Nach tatsächlicher HTTPS-Readiness zeigt die Bridge-Konsole nutzbare URLs, den vollständigen SPKI-SHA-256-Fingerprint sowie die Readiness-Meldung „Bridge läuft; zurück zur Haupt-App und Verbindung testen“. Ein frische ungekoppelte Bridge erzeugt **keinen** automatischen Code und öffnet **kein** Zeitfenster; erst ein ausdrücklicher codefreier Hallo-Kontakt der Haupt-App (oder ein Explizitstart mit `--pair` für Betreiber/Services) löst den sprechbaren kurzlebigen Einmal-Pairingcode aus (12 Zeichen in Vierergruppen, zwei Minuten, maximal fünf Fehlversuche, Einmalverbrauch, Replay-Schutz). Die Antwort des Hallo enthält niemals ein Geheimnis und folgt dem strengen Schema `pairing_required`/`already_paired`; Wiederholungen werden gedrosselt und rotieren oder verlängern ein aktives Fenster nicht; eine bereits gekoppelte Bridge öffnet kein neues Fenster (Reset/Neu-Kopplung bleiben ausdrücklich über `--revoke`/`--pair`). Nach Ablauf erfolgt die Neuerzeugung bewusst erneut über Hallo oder `--pair`-Neustart. Spätere Starts laden die persistierte Authentifizierung direkt. Main vergleicht den Fingerprint bewusst (Checkbox) und koppelt dann; ein Zertifikats-/Pin-Wechsel wird nie stillschweigend vertraut. **Bridge-Pairingcode, Chat-Binding-Code und Tunnel-Credentials sind drei getrennte Welten.** Keine Geheimnisse in URLs, Befehlsargumenten oder Logs; der Code ist ablesbar/eingebbar – keine Pflicht zu Copy/Paste zwischen getrennten Rechnern.

**Qubes/Qrexec-Führung (Bridge-first, Operatorentscheidung 07.10.2026).** Qubes wird über `/usr/share/qubes/marker-vm` bzw. `$QUBES_VM_NAME` erkannt; der Qrexec-Modus erscheint nur dann, Remote-HTTPS bleibt immer verfügbar. Zuerst wird die Bridge vorbereitet: dieselbe Python-Datei in den Ziel-Qube übertragen (z. B. qvm-copy/eigener Weg – es gibt keine geprüfte Release-URL, die Datei ist die Distribution) und dort exakt `python3 ./admin_in_the_loop.py --bridge` starten; die Erstkonfiguration läuft automatisch (State/TLS unter `~/.local/state/aitl-bridge`, SANs, nutzbare URLs, SPKI; der Einmal-Pairingcode erscheint auf der Bridge-Konsole erst nach ausdrücklichem Hallo der Haupt-App oder bei `--pair`). Tunnel-/Runtime-Angaben gehören zur Bridge-Umgebung und werden nur gefordert, wenn dort noch kein Tunnel besteht – bestehende Konfiguration wird wiederverwendet; keine erfundene App-ID-Pflicht, keine Secrets in Chat/Argumenten/Logs. Zurück in der Haupt-App folgt eine primäre Aktion **„Verbindung prüfen“**: sie startet die eigene `qvm-connect-tcp <port>:<ziel>:<port>`-Weiterleitung idempotent (Single-Instance, keine Übernahme/Beendigung fremder Prozesse, sauberer Shutdown, Restart-Wiederherstellung aus gespeicherter Konfiguration) und belegt die echte Erreichbarkeit der Bridge über unauthentifizierten TLS-Handshake plus erwartete Verweigerung des authentifizierten Endpoints – Erreichbarkeit ist weder Vertrauensbestätigung noch fertige Kopplung; das lokale Binding allein ist kein Erfolg. Die exakte, eng begrenzte Policy-Zeile (`qubes.ConnectTCP +<port> <quell-qube> <ziel-qube> allow`, Platzhalter `personal` bei unbekannter Quelle) und die einmalige dom0-Anleitung (z. B. `/etc/qubes/policy.d/30-user.policy`) erscheinen **nur bei Bedarf** – vorhandene Freigaben werden nicht angezweifelt; keine pauschale „Policy fehlt“-Behauptung: belegte Fehler (Tool fehlt, fremder Port, Forwarder-Exit, kein TLS-Dienst, Pin-Wechsel) werden getrennt von möglichen Ursachen (Freigabe offen vs. Ziel/Bridge down) benannt; privilegierte Policy wird niemals automatisch geändert oder umgangen. Nach manueller Korrektur dient dieselbe Aktion erneut als „Erneut prüfen“. Am Ende steht das bewusste Koppeln: Fingerprint unabhängig im Ziel-Qube vergleichen (der beobachtete SPKI wird erst nach dieser Bestätigung angezeigt, die Pin-Eingabe bleibt manuell), vollständigen Pin und Code eingeben und bestätigen. Codegültigkeit bleibt zwei Minuten bei maximal fünf Fehlversuchen; bei Ablauf bewusst lokal neu erzeugen (erneutes Hallo oder `--pair`-Neustart), Limits werden nie gelockert; der Hallo selbst autorisiert weder Pairing noch Vertrauen noch Befehle. HTTPS/Auth bleiben erhalten; keine zusätzlichen Firewallregeln.

**Tunnel und erster Roundtrip.** Tunnel-ID und Restricted-Runtime-Key werden lokal in der Bridge-Umgebung erfasst und bei Neustart wiederverwendet; Produktions-SSH-Schlüssel bleiben im Admin-Qube und gehören nie in Chat, Befehlsargumente oder Logs. Bridge-Erreichbarkeit, Tunnel-Readiness und echte ChatGPT-/Provider-Verifikation bleiben getrennte Belege. Der geführte harmlose Test (Vorschlag → manuelle Befehlsfreigabe → manuelle Ausgabe-Freigabe → Roundtrip) behält alle bestehenden Review- und Unsicherheits-Sperren. Native Qubes-/Qrexec-/echte SSH-/E2E-Abnahme ist separate externe Evidenz; lokale Mock-/Prozess-Tests weisen sie nicht nach.

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
