# Admin in the Loop – MVP 0.8 Testbericht

Stand: 30. September 2026. Alle 86 Tests bestanden. Keine Veröffentlichung im GitHub-Repository.

## Ergebnisse

| Suite | Tests | Sekunden | Protokoll |
|---|---:|---:|---|
| Bestehende PTY-/Controller-Suite | 49 | 7.326 | `Local_Integration_HEADLESS_TESTS.txt` |
| Verbundene Headless-Integration | 36 | 18.828 | `Connected_MVP_HEADLESS_TESTS.txt` |
| Tkinter-/xterm-Smoke-Test | 1 | 0.904 | `GUI_SMOKE_TEST.txt` |

Die Syntaxprüfung aller vier Python-Dateien bestand ebenfalls. Tests nutzen Python-`unittest`/`mock` ohne externes Testframework. Headless bedeutet hier ohne Desktop; reale PTYs, Unix-Sockets, HTTP-/TLS-Sockets und Subprozesse werden trotzdem betrieben.

## Tatsächlich geprüfte Eigenschaften

- Lokaler PTY-Helfer, manuelle Terminalbedienung, Passwort-Echo, Ctrl-C, Resize, EOF, Rückstau und Teilwrites; keine Ausführung oder wiederholte Übertragung ohne gebundenen Review.
- SINGLE/SMALL/MEDIUM/LARGE, persistentes finales Return, konservative Ausgabeauswahl, unveränderte Payload bei weichen Darstellungsumbrüchen, neue IDs nach Bearbeitung.
- Reale HTTP-/MCP-Bridge mit Rollen, festen Schemata, Bodylimits, Cursorprüfung, Einmal-Kopplungscode, Ablauf und SQLite-Retention. Fehlermeldungen reflektieren keine Credentials/Payloads.
- Alle vier MCP-Tools verlangen den expliziten Chat-Code. Ein weiterer App-Chat ohne diesen Code kann ihn nicht erfragen und keinen Output lesen oder Vorschlag zustellen.
- Vollständiger Ablauf MCP-Vorschlag → lokale Review/Freigabe → reale Bash/PTY → Outputreview/Freigabe → MCP-Ausgabe. Empfangsstatus behauptet keine Ausführung.
- Vorschläge ersetzen keinen laufenden Review. Verbindungsverlust, geänderte Generation, abgelaufene Nachricht und gestoppter Netzwerkworker blockieren alte Freigaben. Eine manuelle Shell bleibt unabhängig bedienbar.
- Ungewisse Output-Zustellung: identischer Body/Idempotenzschlüssel bei explizitem Retry, keine doppelte Bridge-Aufnahme. Kein automatischer Retry eines Terminalcommands.
- Echter lokaler TLS-Handshake mit vollständigem SPKI-Pin-/Namensvergleich vor HTTP-Credentials; Remote-Kopplung, sichere Speicherung, Widerruf, Zertifikaterneuerung mit gleichem SPKI und Ablehnung von Remote-Klartext.
- Streaming-Sanitizer für fragmentiertes UTF-8, OSC/CSI/DCS, unvollständige Sequenzen, begrenzte Speicherhaltung und Verlustkennzeichnung.
- 64-KiB-Output ist über MCP abrufbar. Zu große JSON-Hüllen werden explizit gesperrt statt gekürzt. Text wird nur einmal in einem eigenen Contentblock ausgegeben.
- Der echte offizielle `tunnel-client-runtime` v0.0.15 startete mit privaten Credential-Dateien und eingeschränkter Umgebung. Start/Health/Stop und ein kompletter human-gesteuerter Round Trip durch diesen Client bestanden gegen eine lokale simulierte Kontrollplattform.
- Echter Tkinter-/xterm-Test unter Xvfb: Zeilenumbruch ohne horizontale Scrollbar, relative Fensterbreite, gemeinsame Footerhöhe, Return-Checkbox je Richtung, lokaler und MCP-Vorschlag, Einstellungen. Ein OSC-52-Command verändert das isolierte Test-Clipboard nicht.

## Umgebung

Ubuntu 24.04.3 LTS, Linux x86_64; Python 3.12.14, aiohttp 3.13.5, cryptography 46.0.0, Tk 9.0.4, xterm 390. Der offizielle Tunnel-Client wurde aus dem OpenAI-Release v0.0.15 bezogen. Release-Archiv-SHA-256: `f26f8b3ee6c335e38fa5cfbe6ce5635f53738f08a26eecf07d6cebacab4a1abf`; vor Ausführung gegen den Release-Asset-Digest geprüft. Das Binary ist nicht Bestandteil des Projektpakets.

## Grenzen des Nachweises

Die Tests simulieren die bewussten menschlichen Entscheidungen über die lokale API oder GUI-Buttons. Sie können keine menschliche Aufmerksamkeit beweisen. Die lokale Kontrollplattform ist kein echter OpenAI-/ChatGPT-Tunnel. Für diese Abnahme fehlen die eigene Tunnel-ID, Runtime-Credentials und Workspace-Zuordnung. „Client bereit“ bedeutet lokale Bereitschaft.

Zum Berichtsstand 30. September 2026 waren native macOS-/Terminal.app-, praktische Debian-Desktop-, Qubes-/Gateway- und reale SSH-/sudo-/TUI-Abnahmen nicht belegt; ebenso lag kein unabhängiger Review des konkreten Betriebsprofils für kritische Infrastruktur vor. Aktuelle Arbeit zu diesen Punkten wird ausschließlich über Taskboard-Tickets gesteuert. Gleiches Benutzerkonto ist keine starke Prozess-/Dateisystemisolation. Der Chat-Code ist eine Bearer-Bindung, keine kryptografisch bestätigte Provider-Chat-ID. Outputintervalle sind keine verifizierten Commandgrenzen oder Exitcodes.

Zum Berichtsstichtag war der Orchestrator-Skill erstellt und validiert; die automatische Freigabeprüfung blockierte seine dauerhafte externe Installation mangels ausdrücklicher Zustimmung. Dieser Schritt wurde nicht umgangen. Der aktuelle Installationsstatus wird ausschließlich im Taskboard geführt. Automatische Risk-/Secret-Erkennung und ein eingebetteter Terminalemulator sind kein Bestandteil dieses MVP.

## SHA-256 der geprüften Python-Dateien

| Datei | SHA-256 |
|---|---|
| `admin_in_the_loop.py` | `7166e1ba554e0a85ebc4f35b340664701d159ba33c65e8b203baf9c889487276` |
| `aitl_bridge.py` | `495bda6ed08e608c57e52fee8f0efe2c9e35817a8d37c2c50703a5417211e746` |
| `test_connected_mvp.py` | `c9a842395b054b49435796b0be53f9206d2de5b67dc222a5e66e9fac725290a0` |
| `gui_smoke_test.py` | `2b5d44fec9d66612bce058661de75a57121b15030a7a2e2e0067fd852570c476` |
