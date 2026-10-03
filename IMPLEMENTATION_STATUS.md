# Admin in the Loop – Implementierungsstand 0.8

> **Historischer Implementierungs- und Evidenzstand – kein aktiver Backlog.** Diese Momentaufnahme vom 30. September 2026 bewahrt Implementierungsbehauptungen, damalige Prüfnachweise und deren Grenzen. Sie ist nicht die aktuelle Quelle offener Arbeit und darf nicht als parallele Todo-Liste verwendet werden. **Alle aktive Implementierungs-, Validierungs- und Abnahmearbeit wird ausschließlich über Taskboard-Tickets gesteuert.** Das maßgebliche Produkt-/Konzeptdokument ist [`doc/Admin-in-the-Loop_Konzept_V0.7.md`](doc/Admin-in-the-Loop_Konzept_V0.7.md); die historische Ticketzuordnung aus der Planungsmigration steht in [`doc/TASKBOARD_PROPOSAL.md`](doc/TASKBOARD_PROPOSAL.md). Ticketstatus, aktive Aufgaben und ihre Reihenfolge sind ausschließlich im Taskboard maßgeblich.

Stand: 30. September 2026. Controller und PTY-Helfer bleiben in einer Python-Datei. Die Bridge ist als separates Programm implementiert und importiert den Controller nicht. Keine Veröffentlichung im reservierten GitHub-Repository.

| Paket | Umsetzung | Nachweis / Grenze |
|---|---|---|
| PTY, Terminal-Helfer, lokale IPC | Implementiert | Reale Linux-PTY-/Socket-Tests; macOS-Abnahme offen |
| Drei-Spalten-GUI, Review, vertikales Scrollen | Implementiert | Echter Tkinter/xterm-Smoke-Test bestanden; separater Terminalbereich bleibt eigenes Fenster |
| Commands und Output lokal prüfen/freigeben | Implementiert | Einzelne unveränderliche Reviews, Return-Präferenz, SINGLE bis LARGE |
| HTTP-Bridge, Long-Polling, Netzclient | Implementiert | Getrennter Prozess, feste Schemata, Limits, Backoff und Fehlerzustände |
| MCP-Adapter und Chatbindung | Implementiert | Vier Tools verlangen den menschlich ausgewählten Chat-Code; keine Terminal-/Datei-/Ausführungsfähigkeiten |
| OpenAI-Tunnel-Lifecycle | Implementiert | Offizielles Binary v0.0.15: Start/Health/Stop und kompletter lokaler Round Trip; kein automatischer Download |
| TLS-SPKI-Pinning, Remote-Kopplung, Widerruf | Implementiert | Reale lokale TLS-Tests, vollständige Pinprüfung vor HTTP; Operatorvergleich bleibt menschlich |
| Gespeicherte Verbindungen und Präferenzen | Implementiert | 0700/0600, atomarer Austausch; kein Wiederherstellen von Freigaben |
| Output-Zustellung und Retry | Implementiert | Empfängergebunden, derselbe Body/Idempotenzschlüssel, maximal 24 h |
| Sanitizer | Implementiert | Streamingfähige begrenzte Projektion; kein Vollterminalrenderer |
| Headless-Kern und Integrationstests | Implementiert | 86 Tests bestanden: 49 bisherige, 36 verbundene, ein GUI-Test; Standardbibliothek für Tests |
| Orchestrator-Skill | Erstellt und validiert | Dauerhaftes Speichern im persönlichen Skill-Verzeichnis durch automatische Freigabeprüfung blockiert |
| Onboarding und erster Round Trip | Implementiert / beschrieben | Lokaler MCP-Round-Trip getestet; echter ChatGPT/OpenAI-Round-Trip benötigt eigene Tunnelkonfiguration |
| Qubes/Gateway | Architektur und Betriebsanleitung | Remote-Betrieb technisch implementiert; keine Qubes-/Hardware-Abnahme in dieser Umgebung |

## Verbleibende externe Abnahmen

- Echter ChatGPT-Round-Trip mit selbst eingerichteter Tunnel-ID, Runtime-Key und passender Workspace-Zuordnung.
- Native macOS-/Terminal.app-Abnahme und praktische Debian-Desktop-Abnahme; diese Umgebung ist Ubuntu 24.04.
- Reale SSH-/sudo-/TUI-Bedienprobe und Sicherheitsreview des konkreten Betriebsprofils.
- Qubes-/Gateway-Isolation einschließlich Firewall, Dateien, Agenten und Clipboard prüfen.
- Orchestrator-Skill nach ausdrücklicher Zustimmung dauerhaft installieren.

Diese Punkte sind keine erfundenen Testergebnisse. Automatische Risk-/Secret-Erkennung, exakte Befehlsgrenzen/Exitcodes und ein eingebetteter Vollterminalemulator bleiben außerhalb des MVP.

## Prüfnachweise und Grenzen

Alle 86 Prüfungen bestanden am 30. September 2026. Ubuntu 24.04/Linux x86_64, Python 3.12.14. Der offizielle Tunnel-Client v0.0.15 kommunizierte mit einer lokalen simulierten Kontrollplattform; ein echter OpenAI-/ChatGPT-Tunnel wurde mangels eigener Konfiguration nicht eingerichtet. „Client bereit“ bezeichnet lokale Bereitschaft.

Gleiche Benutzerkennung isoliert die Bridge nicht zuverlässig vom Dateisystem des Controllers. Die Chatbindung ist ein bewusst geteilter Bearer-Code, keine vom Provider bestätigte Chat-ID. Command-Marker beschreiben Beobachtungsintervalle und erkennen keinen sicheren Befehlsabschluss. Risiko-Classifier, automatische Secret-Redaction, eingebetteter Terminalrenderer und Qubes-RPC sind weiterhin außerhalb dieses MVP.

Die damalige Behauptung „keine zusätzlichen Python-Pakete“ galt seinerzeit für den lokalen Controller und seine Tests; die separate Bridge benötigte damals das Debian-Paket `python3-aiohttp`, Remote-TLS zusätzlich `python3-cryptography`. Kein externer Testframework- oder Agentenstack.

**Update Task task_98110f66 (HTTP-/Event-Layer-Konsolidierung):** Die Bridge nutzt seit dieser Änderung ausschließlich die Python-Standardbibliothek (`http.server`-basiert); `python3-aiohttp` entfällt. Remote-TLS-Zertifikatsgenerierung benötigt weiterhin `cryptography`. Neu hinzugekommen ist die MCP-Events-API (nur Protokollversion 2026-07-28): `events/list`, `events/subscribe`, `events/unsubscribe` mit signierten Standard-Webhooks, Callback-Verifikation, SSRF-Schutz und kanonischem Delivery-State in derselben outputs-Tabelle; der Pull-Pfad (get_submission_status/get_approved_output) bleibt robuster Fallback. Ein E2E-Nachweis im echten ChatGPT-Work-Workspace (Event-Continuation ohne zusätzlichen User-Turn) steht noch aus und ist ausdrücklich nicht belegt.
