# Admin in the Loop – Taskboard-Handoff und Vorschlagshistorie

Stand: 30. September 2026. **Taskboard-Handoff abgeschlossen. Dieses Dokument ist ein historischer Migrationsnachweis, kein aktiver Backlog und keine zweite Quelle für Ticketstatus.**

Maßgeblicher Produkt-/Konzeptbezug: [Admin-in-the-Loop_Konzept_V0.7.md](Admin-in-the-Loop_Konzept_V0.7.md), inhaltlich auf MVP 0.8 fortgeschrieben. Der Ordner heißt im Repository `doc/`; die einzige mitgelieferte Konzeptrevision war V0.7, deren Inhalt V0.3–V0.6 ausdrücklich ersetzt. [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) ist ein historischer Implementierungs-/Evidenzstand. Die untenstehenden Beschreibungen erhalten die damalige Priorisierungs- und Deduplizierungsrationale; für alle aktive Arbeit, Ticketdetails, Zustände und Reihenfolge ist ausschließlich das Taskboard maßgeblich.

**Abgeschlossener Handoff:** Die Board-Abdeckung und IDs in §2 wurden vom Operator als vorhanden/verifiziert bereitgestellt. Sie werden hier als Referenzen festgehalten; in diesem Dokumentationsschritt wurde das Board nicht verändert oder unabhängig abgefragt. Zwei nach der ursprünglichen Synthese hinzugekommene aktive Tickets sind zusätzlich in §2 aufgeführt. Die detaillierten Tickettexte und deren aktueller Zustand liegen ausschließlich im Board.

## 1. Historische Prioritätsregel und Erfassungsgrenze

- **High:** Die fünf ausdrücklich aufgeführten Folgeaktionen unter `IMPLEMENTATION_STATUS.md`, „Verbleibende externe Abnahmen“, Zeilen 24–28. Die Plattformzeile und die Bedienprobe-/Reviewzeile enthalten je zwei getrennt abschließbare Pflichten; daraus werden sieben Tasks. High bezeichnet hier den beauftragten Status-Follow-up, nicht eine erfundene Sicherheitsdringlichkeit. Ausführung bleibt an verfügbare Prüfgeräte, eigene Credentials beziehungsweise Zustimmung gebunden.
- **Medium:** Weitere dort dokumentierte offene Pflichten, die keine ausdrücklichen Folgeaktionen sind. **Keine zusätzlichen gefunden:** Die offenen Tabellenhinweise in Zeilen 7, 18–20 und die Grenzen in Zeilen 34–36 wiederholen die High-Pflichten oder beschreiben bewusste Ausschlüsse. Keine zweite Erfassung derselben Pflicht.
- **Low:** Erst nach Konzeptsynthese ermittelte Restarbeit aus erhaltenen Konzeptanforderungen beziehungsweise der statischen Repository-Inventur. Dazu gehören die Reorganisationsfehler, unabhängig von ihrer praktischen Ausführungsreihenfolge. Eine technische Voraussetzung für einen High-Test wird deshalb nicht automatisch auf High hochgestuft.

H01–H07 und L01–L09 waren lokale Zuordnungsschlüssel der initialen Planung. Die nachträglich bestätigten tatsächlichen Board-IDs stehen in §2. Die folgende Prioritäts- und Kandidatenrationale ist als Migrationsevidenz erhalten und ist keine Aufforderung, zusätzliche Tasks anzulegen. Ein Task hatte in der Planung genau einen Home; Quellen konnten ihn mehrfach erwähnen. „Unabhängig abschließbar“ beschreibt den damaligen Entwurf, nicht den aktuellen Boardstatus.

Datei-/Zeilennummern in den historischen Quellenangaben bezeichnen den Stand bei der ursprünglichen Synthese. Seither eingefügte Archivhinweise haben einige Zeilennummern verschoben; der zugeordnete Sachverhalt und die benannten Abschnitte bleiben der Quellbezug.

Die ursprüngliche Vorschlagsmethodik forderte Ausführungsnachweise mit Umgebung, Schritten, Ergebnis und Grenzen, ohne echte Credentials, Chat-Codes oder unbeabsichtigte Terminalinhalte. Ein blockierter Test war kein bestandener Test; nötige Reparaturen nach einem negativen Abnahmebefund sollten anhand des tatsächlichen Befunds gesondert geplant werden. Das ist hier als historische Methodik wiedergegeben, nicht als zusätzlicher Backlog.

## 2. Board-Referenzen des abgeschlossenen Handoffs

Die IDs unten sind die vom Operator bereitgestellte, verifizierte Board-Abdeckung der ursprünglichen Vorschlagskennungen. Der aktive Arbeitsstatus liegt auf dem Taskboard; diese Tabelle ist nur ein historisches Querverzeichnis.

| Vorschlagskennung | Taskboard-Ticket |
|---|---|
| H01 | `task_6a0ae75c-6ccf-5ed5-a8a3-3867a7740223` |
| H02 | `task_a11b8b07-4b6e-5977-bc77-c8bcbc9e54d4` |
| H03 | `task_d24c1e09-3877-5b6e-90f0-57e528b57f1b` |
| H04 | `task_16d1b7f2-6101-506c-9ca8-403249c58157` |
| H05 | `task_78cece75-67a3-59ba-8b8c-f0c4a3b2b44a` |
| H06 | `task_7cfd7f53-cfcd-55e9-947e-d4168c05a74d` |
| H07 | `task_32f9567e-e18d-5ac2-b75c-b40ef19b658d` |
| L01 | `task_07be7b4c-608c-5f41-9e23-fa2031323e0f` |
| L02 | `task_38825050-ea04-5ba8-90b4-1dd6b2c3f694` |
| L03 | `task_7c4bc47c-53ee-584e-95fb-1032289c9650` |
| L04 | `task_112dbbf3-52c3-5e2d-8aa5-637cd1ccd165` |
| L05 | `task_60c0acf2-286e-5e1d-afe3-017a1cadb73b` |
| L06 | `task_3bb7b8f4-5873-57b8-92ea-05620eda384d` |
| L07 | `task_a74dab76-760a-5846-92e5-d906c98f5f5f` |
| L08 | `task_e2b4e051-ec5e-56cd-ae6c-19d79f8539d0` |
| L09 | `task_4cc0ff38-b5a5-56ad-b2e6-e64459c3ec54` |

Zusätzlich gemeldete aktive Tickets nach der ursprünglichen Synthese; ihre Anforderungen werden nicht hier dupliziert:

| Boardbezeichnung | Taskboard-Ticket |
|---|---|
| AI-assisted onboarding | `task_17faaddd-e3dd-57ed-8d10-0f288c93bef1` |
| Single Python entry point / controller+bridge consolidation | `task_4d947ee7-bf97-5f6c-8e8a-ad485a744dde` |

## 3. Historische High-Kandidaten – explizite Status-Folgepflichten

### H01 – Echten ChatGPT-/OpenAI-Round-Trip abnehmen

- **Priorität:** High.
- **Beschreibung / Abschluss:** Mit eigener Tunnel-ID, Runtime-Key und korrekter App-/Workspace-Zuordnung den offiziellen Client betreiben. In einer bezeichneten Testshell einen harmlosen MCP-Vorschlag empfangen, den Command lokal prüfen/freigeben, ausgewählten Output separat prüfen/freigeben und im gewählten Chat tatsächlich zurücklesen. Routing, zwei getrennte Freigaben und beobachtetes Ergebnis dokumentieren. Client-Readiness oder lokaler Simulator genügt nicht. Direkter MCP-Ablauf ist zulässig; keine Pflicht zur vorherigen Skill-Installation.
- **Quellen:** `IMPLEMENTATION_STATUS.md:19,24,34`, Abschnitte „Verbleibende externe Abnahmen“ und „Prüfnachweise und Grenzen“; Konzept §12.1 „Tunnel, Skill und erster Round Trip“, §17 „Abnahme und Implementierungsreihenfolge“; `tests/MVP_0.8_TEST_REPORT.md:36`.
- **Abhängigkeiten / Reihenfolge:** Eigene Providerkonfiguration und bestätigter lokaler Pfad erforderlich. L01/L02 vorab empfohlen für reproduzierbare Vorbereitung; H07 ist keine harte Abhängigkeit.
- **Unabhängig abschließbar:** Ja, eigener Cloud-Nachweis.
- **Vorgeschlagener Workstream:** Verbundener MCP-/Orchestratorbetrieb.

### H02 – Native macOS-/Terminal.app-Abnahme durchführen

- **Priorität:** High.
- **Beschreibung / Abschluss:** Auf nativem macOS mit Python ≥3.11/Tk die dokumentierte Einrichtung und den Terminal.app-/osascript-Start einschließlich Automation-Berechtigungen prüfen. PTY-Bedienung, Resize, Signale/EOF, manuellen Fallback, beide Reviews, Return-Persistenz und lokale Verbindung prüfen. Konkrete Terminalversion auf unerwünschte Clipboard-/Fenster-/Linkfunktionen und automatische Protokollantworten untersuchen; verbleibende Grenzen ausweisen. Kein Linux-/Xvfb-Ergebnis als macOS-Nachweis verwenden.
- **Quellen:** `IMPLEMENTATION_STATUS.md:7,25`; Konzept §§4–6, §16 „Feindlicher Output und spätere Erweiterungen“, §17; `tests/MVP_0.8_TEST_REPORT.md:38`.
- **Abhängigkeiten / Reihenfolge:** Native macOS-Prüfumgebung erforderlich; L01/L02 empfohlen. Die interaktive SSH-/sudo-/TUI-Matrix gehört ausschließlich H04, das unabhängige Sicherheitsurteil H05.
- **Unabhängig abschließbar:** Ja, eigener Plattformbericht.
- **Vorgeschlagener Workstream:** Plattform- und Bedienabnahme.

### H03 – Praktische Debian-Desktop-Abnahme durchführen

- **Priorität:** High.
- **Beschreibung / Abschluss:** Die im Konzept genannten Debian-12/13-Ziele mit tatsächlicher grafischer Sitzung und dokumentierten Python/Tk/xterm-, aiohttp- und TLS-Abhängigkeiten prüfen. Installation/Launcher, manuellen Fallback, PTY/Resize/Signale, Reviewlayout und lokalen verbundenen Pfad dokumentieren. Konkrete xterm-Version und deaktivierte Steuerfunktionen einschließlich Clipboard sowie automatische Protokollantworten prüfen. Ergebnisse und bestätigte beziehungsweise nicht bestätigte Release-Abdeckung festhalten; Ubuntu/Xvfb ersetzt diese Abnahme nicht.
- **Quellen:** `IMPLEMENTATION_STATUS.md:25,38`; Konzept §5 „GUI und Installation auf beiden Plattformen“, §§16–17; `tests/Local_Integration_README.md:224`.
- **Abhängigkeiten / Reihenfolge:** Tatsächliche Debian-Desktop-Prüfumgebung erforderlich; L01/L02 empfohlen. SSH-/sudo-/TUI-Matrix nicht duplizieren: H04.
- **Unabhängig abschließbar:** Ja, eigener Plattformbericht; fehlende Zielrelease-Abdeckung bleibt ausdrücklich offen.
- **Vorgeschlagener Workstream:** Plattform- und Bedienabnahme.

### H04 – Reale SSH-/sudo-/TUI-Bedienprobe abschließen

- **Priorität:** High.
- **Beschreibung / Abschluss:** In einer kontrollierten realen Admin-Prüfumgebung SSH, sudo/Passwortprompt, Echo an/aus, offene Shell-Quote, vim/top, Hintergrundoutput und gleichzeitige manuelle Eingabe erproben. Nachweisen, dass unbekannter Prompt nur einen lokalen Override erlaubt, Freigaben nicht wiederverwendet werden und Outputintervalle keine sicheren Commandgrenzen/Exitcodes behaupten. Interleaving, Bediengrenzen und tatsächlich geprüfte Kombinationen dokumentieren. Keine Secret-Erkennung oder Shell-Hooks implementieren.
- **Quellen:** `IMPLEMENTATION_STATUS.md:26,36`; Konzept §§6,8–10,17, insbesondere P1-Bedienprobe und kritische Testmatrix.
- **Abhängigkeiten / Reihenfolge:** Mindestens eine lokal akzeptierte Plattform aus H02/H03 und ein kontrolliertes SSH-/sudo-Ziel erforderlich. Weitere vorgesehene Plattformkombinationen separat im Bericht kenntlich machen; H05 nutzt den Befund.
- **Unabhängig abschließbar:** Ja, eigener Bedienbericht; keine Sicherheitszertifizierung.
- **Vorgeschlagener Workstream:** Plattform- und Bedienabnahme.

### H05 – Konkretes Betriebsprofil unabhängig sicherheitlich reviewen

- **Priorität:** High.
- **Beschreibung / Abschluss:** Die konkrete Umsetzung und vorgesehene Nutzung unabhängig reviewen: einmalige Snapshot-/Sitzungs-/Empfängerbindung, Retry-Abgrenzung, TLS-Prüfung vor Credentials, Bearer-Chatbindung, Credential-/Datei-/Prozessgrenzen, Log-/Retentiongrenzen und Terminal-Vertrauensbasis. Plattform-/Bedienbefunde und bei getrennter Nutzung H06 einbeziehen. Feststellungen, Restvertrauen und zulässiges beziehungsweise nicht abgenommenes Nutzungsprofil dokumentieren. 86 Tests sind kein Sicherheitsurteil.
- **Quellen:** `IMPLEMENTATION_STATUS.md:26,36`; Konzept §§1,13–17; `tests/MVP_0.8_TEST_REPORT.md:38`.
- **Abhängigkeiten / Reihenfolge:** Profil und unabhängige Reviewperson benennen. H04 und relevante H02/H03-Befunde vor dem abschließenden Urteil; H06 vor Abnahme eines Qubes-/Gateway-Profils. Reparaturen erfolgen nur anhand konkreter Befunde in späteren Aufträgen.
- **Unabhängig abschließbar:** Ja, eigenes Reviewurteil; H06 liefert technische Isolationsevidenz, ersetzt diesen Review nicht.
- **Vorgeschlagener Workstream:** Getrennter Betrieb und Sicherheitsabnahme.

### H06 – Qubes-/Gateway-Isolation praktisch prüfen

- **Priorität:** High.
- **Beschreibung / Abschluss:** Für die vorgesehenen getrennten Qubes-/Gateway-Profile Controller/Helfer/Terminal und Bridge/Tunnel real trennen. Firewallrichtung, keine gemeinsamen Admin-Homes/Dateien, kein PTY-/Socket-/Capability-Zugriff, keine SSH-Agent-Vererbung und kein automatisch geteilter Clipboard-Zugang prüfen. Qubes im App-Qube, nicht dom0; Remote-MCP-Port bleibt Loopback, Gateway betreibt den Tunnel. Prüfmatrix je vorgesehenem Profil dokumentieren; ungetestete Profile bleiben ausdrücklich unabgenommen. Keine Qubes-RPC-Implementierung aus dieser Prüfung ableiten.
- **Quellen:** `IMPLEMENTATION_STATUS.md:20,27,36`; Konzept §§12.1,15 „Betriebsprofile und Datenhaltung“,16–17.
- **Abhängigkeiten / Reihenfolge:** Reale Qubes-/Gateway-Prüfumgebung und benannte Profile erforderlich. Nach lokalem Plattformpfad, vor H05 für kritischen getrennten Betrieb.
- **Unabhängig abschließbar:** Ja, eigener Isolationsbericht; unabhängiges Sicherheitsurteil bleibt H05.
- **Vorgeschlagener Workstream:** Getrennter Betrieb und Sicherheitsabnahme.

### H07 – Aktuelle Orchestrator-Skill-Beilage nach Zustimmung installieren

- **Priorität:** High.
- **Beschreibung / Abschluss:** Ausdrückliche Zustimmung zur persönlichen Speicherung und das Zielsystem/-verzeichnis festhalten. Die aktuelle `skill/SKILL.md` unter ihrem Skillnamen `admin-in-the-loop-orchestrator` im Zielsystem installieren und Auffindbarkeit/Toolbezug prüfen. Kein Credential in Beispiele übernehmen und die frühere Freigabespeicherungssperre nicht umgehen. Die rekonstruierte Beilage nicht als wiedergefundene Originaldatei ausgeben. Ein Live-Verhaltensnachweis ist separat L07.
- **Quellen:** `IMPLEMENTATION_STATUS.md:18,28`; Konzept §12.1 und §17; `skill/HANDOFF_SKILL_NOTE.md:7–18`.
- **Abhängigkeiten / Reihenfolge:** Zustimmung und Zielverzeichnis erforderlich; L02 zur Pfadklarheit empfohlen. H01 ist keine Installationsvoraussetzung.
- **Unabhängig abschließbar:** Ja, installierter und auffindbarer Skill; keine Behauptung eines bestandenen Live-Workflows.
- **Vorgeschlagener Workstream:** Verbundener MCP-/Orchestratorbetrieb.

## 4. Historische Medium-Prüfung

**Keine zusätzlichen Tasks.** Alle im Status benannten offenen Pflichten sind bereits genau einmal in H01–H07 vertreten. Tabelle und Testgrenzen werden nicht als zweite Aufgabenliste angelegt. Bewusste MVP-Ausschlüsse bleiben Ausschlüsse.

## 5. Historische Low-Kandidaten – Konzept- und Repository-Arbeit

### L01 – Verschobene Testimporte und Einstiegspfade reparieren

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Ein konsistentes, minimal ausgestaltetes Aufruf-/Importmodell für `tests/test_connected_mvp.py` und `tests/gui_smoke_test.py` festlegen und reparieren. Rootmodule müssen ohne zufälligen PYTHONPATH erreichbar sein; der GUI-Geschwisterimport muss zum gewählten Modell passen. Bestehende Controller-, Integrations- und GUI-Suites danach in einem gesondert autorisierten Ausführungspass prüfen. Historische 49+36+1 Ergebnisse erhalten; zwei optionale Tunneltests ohne Binary als übersprungen statt bestanden ausweisen. Keine neue Testsuite, CI oder Paketstruktur allein zum Selbstzweck einführen.
- **Quellen:** Konzept §19.2 „Repository-Anordnung und offene Pfadkonsistenz“; `tests/test_connected_mvp.py:25–26`, `tests/gui_smoke_test.py:14–15`, Tunnel-Skipbedingungen `tests/test_connected_mvp.py:598,720`; abgeschlossene Inventur. Diese Reparatur steht nicht in IMPLEMENTATION_STATUS und erhält deshalb Low.
- **Abhängigkeiten / Reihenfolge:** Keine andere Task-Abhängigkeit. Früh ausführen, L02 an den gewählten tatsächlichen Aufruf anpassen. Tests erst mit späterer Ausführungsautorisierung.
- **Unabhängig abschließbar:** Ja, reproduzierbare bestehende Testeinstiege mit aktuellem Ergebnis.
- **Vorgeschlagener Workstream:** Ungruppiert, querschnittliche Repository-Konsistenz.

### L02 – Reorganisierte Anleitungs- und Skillpfade konsistent machen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Dateitabelle und Arbeitsverzeichnis in `tests/Local_Integration_README.md` klären; alte Testaufrufe einschließlich Tunnel-/GUI-Variante und GUI-Docstring an L01 anpassen. In `skill/HANDOFF_SKILL_NOTE.md` den ehemaligen `skills/admin-in-the-loop-orchestrator/SKILL.md`-Pfad berichtigen; den Skill-Verweis auf `tests/Local_Integration_README.md` präzisieren. Provenienz der rekonstruierten Beilage erhalten. Alle aktiven Verweise auf tatsächlich vorhandene Ziele prüfen. Die spätere Entfernung des Statusverweises gehört nur L09.
- **Quellen:** Konzept §§12.1,19.2 „Repository-Anordnung und Reorganisationsbefunde der Erstinventur“; `tests/Local_Integration_README.md:13–17,206,212,219`, `tests/gui_smoke_test.py:2`, `skill/HANDOFF_SKILL_NOTE.md:5`, `skill/SKILL.md:53`; abgeschlossene Inventur.
- **Abhängigkeiten / Reihenfolge:** Nach Wahl des Testaufrufmodells in L01; Skillpfade können unabhängig davon korrigiert werden. Vor H07 empfohlen.
- **Unabhängig abschließbar:** Ja, pfadkorrekte operative Dokumentation.
- **Vorgeschlagener Workstream:** Ungruppiert, querschnittliche Repository-Konsistenz.

### L03 – Review-Detailansicht für exakte UTF-8-Bytes ergänzen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Für den bestehenden unveränderlichen Review einen lokal aufrufbaren Detailmodus mit Codepoints und den exakten gebundenen UTF-8-Payloadbytes bereitstellen. LF/Return und Unicode eindeutig zeigen; Darstellungsmarker niemals übertragen oder zur Payloadrekonstruktion verwenden. Byteansicht muss mit demselben Snapshot übereinstimmen und keine neue Freigabe auslösen.
- **Quellen:** Erhaltene V0.7-Anforderung und synthetisiertes Konzept §7 „Inbox und unveränderlicher Review“; `admin_in_the_loop.py:1359–1382,2524–2552` zeigt Codepointprojektion, keine Byte-Detailansicht.
- **Abhängigkeiten / Reihenfolge:** Keine harte Abhängigkeit; nach L01 für gezielte Bytegleichheitsprüfung empfohlen.
- **Unabhängig abschließbar:** Ja, eigene Review-Darstellungsanforderung.
- **Vorgeschlagener Workstream:** Review-Bedienanforderungen.

### L04 – Command-Normalisierung im Review kenntlich machen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Beim Erstellen beziehungsweise Bearbeiten eines Commandreviews sichtbar kenntlich machen, wenn CR/CRLF oder zusammenhängende finale LF normalisiert wurden. Die implementierte Byte-Normalisierung und Return-Erststartpräferenz unverändert lassen; Hinweis nur aus dem tatsächlich eingegangenen Text und finalen Snapshot ableiten. Keine Rohtextanzeige als zweite ausführbare Payload und keine Bearbeitung nach Freigabe.
- **Quellen:** Konzept §8 „Mehrzeilige Blöcke und Return Semantik“, Algorithmusschritt 2 und offener Kennzeichnungsteil; `admin_in_the_loop.py:63–85,2284–2304,2826–2889` normalisiert und zeigt das Ergebnis ohne gesonderte Änderungskennzeichnung.
- **Abhängigkeiten / Reihenfolge:** Keine harte Abhängigkeit zu L03; beide können dieselbe unveränderliche Reviewbasis nutzen.
- **Unabhängig abschließbar:** Ja, eigener Normalisierungshinweis.
- **Vorgeschlagener Workstream:** Review-Bedienanforderungen.

### L05 – Lokale Pause in der GUI bedienbar machen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Den vorhandenen `AppCore.set_paused()`-Mechanismus lokal über eine sichtbare Pause-/Fortsetzen-Aktion samt Pausenzustand zugänglich machen. Neue Command-/Outputfreigaben und Output-Retry während Pause sperren; laufende Shellprogramme oder bereits geschriebene Bytes nicht als widerrufen darstellen. Keine AI-seitige Pause-/Freigabefähigkeit hinzufügen.
- **Quellen:** Konzept §11 „Ressourcen und Bedienungsschutz“; `admin_in_the_loop.py:2401–2403` und Freigabe-/Retryguards; `ReviewUI` ab Zeile 2555 besitzt keine solche Bedienaktion.
- **Abhängigkeiten / Reihenfolge:** Keine harte Task-Abhängigkeit; gezielte lokale Funktionsprüfung nach L01 empfohlen.
- **Unabhängig abschließbar:** Ja, zugängliche vorhandene Pausenfunktion.
- **Vorgeschlagener Workstream:** Review-Bedienanforderungen.

### L06 – Controller-Detach und Terminal-/Helfer-Ende verständlich ausweisen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Die erhaltene Lifecycle-Bedienanforderung erfüllen: Beim lokalen Schließen beziehungsweise Verlust des relevanten Pfads klar erklären, dass Controller-Detach AI-Transfers/Mitschnitt beendet und die manuelle Shell weiterlaufen kann, während Terminal-/Helfer-Ende die PTY-Sitzung beenden kann. Die vorhandene Prozesssemantik bewahren; nicht behaupten, jeder GUI-Close beende die Shell, und keine zusätzlichen Bestätigungsflüsse ohne diese konkrete Anforderung einführen.
- **Quellen:** Konzept §6 „Lokale IPC und normale Bedienung“, Schlussabsätze; `admin_in_the_loop.py:2501–2521,2903–2905` detach/close ohne explizite Lifecycle-Erklärung.
- **Abhängigkeiten / Reihenfolge:** H02/H03-Proben können das genaue plattformspezifische Verhalten liefern; keine Abhängigkeit von einer Backend-Neuentwicklung.
- **Unabhängig abschließbar:** Ja, eigener nachvollziehbarer Lifecycle-Hinweis.
- **Vorgeschlagener Workstream:** Review-Bedienanforderungen.

### L07 – Workflow-Konformität des rekonstruierten Skills beobachten

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Die aktuelle Beilage in einem kontrollierten MCP-Workflow tatsächlich verwenden und Tool-/Argumenttreue, bewusst übergebenen Chat-Code, SINGLE-Präferenz, getrennte menschliche Command-/Outputfreigaben, Metadaten-/Payloadauswertung und konservativen Umgang mit leerem Output sowie untrusted Output-Anweisungen beobachten. Einen unklaren Zustellungsfall auf identischen Request-/Text-Retry statt neuen Command prüfen. Ergebnis und Grenzen dokumentieren; statische Frontmatterprüfung oder Anwendungstests nicht als diesen Nachweis ausgeben.
- **Quellen:** Konzept §12.1; `skill/HANDOFF_SKILL_NOTE.md:7–18` trennt neue Beilage von ursprünglichem Skill und Anwendungstests; `skill/SKILL.md`, Abschnitte „Propose the next command“, „Wait and recover“, „Read deliberately shared output“.
- **Abhängigkeiten / Reihenfolge:** Funktionierender lokaler MCP-Prüfpfad; L01/L02 empfohlen. Kann in einer bewusst eingerichteten temporären Sitzung erfolgen, deshalb keine harte Abhängigkeit von H07 oder echten Cloud-Credentials. Keine Umgehung persönlicher Installationszustimmung.
- **Unabhängig abschließbar:** Ja, Workflowbericht zur konkreten Skilldatei; H07 bleibt reine Installation, H01 echter Providertransport.
- **Vorgeschlagener Workstream:** Verbundener MCP-/Orchestratorbetrieb.

### L08 – Vollständigen SPKI-Fingerabdruck lesbar gruppieren

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Den vollständigen 64-Hexzeichen-SPKI-Pin auf den lokalen Vergleichsflächen einheitlich in 16 Vierergruppen darstellen. Internen 256-Bit-Pin, Prüfung vor HTTP und akzeptierte Eingabeformatierung unverändert lassen. Keine Verkürzung und kein Ersatz des unabhängigen Vergleichs; Darstellung und Rücknormalisierung müssen denselben vollständigen Wert erhalten.
- **Quellen:** Erhaltene V0.7-Festlegung und Konzept §13 „TLS Identität und Kopplung“, Absatz „Identität“; `aitl_bridge.py:693–702,748` gibt vollständigen Pin ungruppiert aus; `admin_in_the_loop.py:2075,2728–2735` akzeptiert Eingabeformatierung/Bestätigung.
- **Abhängigkeiten / Reihenfolge:** Keine harte Task-Abhängigkeit; möglichst vor H06 für die praktische Ableseprobe.
- **Unabhängig abschließbar:** Ja, eigene Darstellungsanforderung ohne neues Kryptografiemodell.
- **Vorgeschlagener Workstream:** Review-Bedienanforderungen.

### L09 – IMPLEMENTATION_STATUS erst nach verifiziertem Board-Handoff auflösen

- **Priorität:** Low.
- **Beschreibung / Abschluss:** Der ursprüngliche L09-Kandidat sah nach Board-Handoff einen gesondert autorisierten Cleanup vor: Konzeptinhalt vollständig, genau ein Board-Home je Pflicht und keine einzigartige erforderliche Statusinformation nur im Statusdokument; tatsächliche Board-IDs dokumentieren, aktive Statusverweise in `tests/Local_Integration_README.md` auf Konzept/Board umstellen und erst danach gegebenenfalls `IMPLEMENTATION_STATUS.md` entfernen. Migrationsverweise sollten als abgeschlossene Provenienz gekennzeichnet sein; historische Testberichte/Protokolle und Nachweise bleiben erhalten. Der Operator ordnete später Handoff und Archivierungstrennung an. Das Statusdokument bleibt gemäß der aktuellen Vorgabe bestehen; dieser Archivtext ändert seinen Zustand nicht. Aktueller Taskstatus ausschließlich im zugeordneten Board-Ticket.
- **Quellen:** Ursprünglicher Planungsmigrationsauftrag, Abschnitt F „IMPLEMENTATION_STATUS deletion gate“; Konzept §19.2, Migrationsabsatz; Dateitabelle in `tests/Local_Integration_README.md` bei der Erstinventur; historischer Handoff-Nachweis §8.
- **Abhängigkeiten / Reihenfolge (damaliger Vorschlag):** Der L09-Kandidat hing von der Erfassung/Verifizierung der Homes für H01–H07 und L01–L09 ab, nicht von deren Umsetzung. Der Operator hat diese Boardabdeckung inzwischen gemeldet. Jeder spätere Löschschritt bleibt von einer gesonderten Autorisierung abhängig und ist in diesem Handoff nicht erfolgt. Aktueller Ticketstatus ausschließlich im Board.
- **Unabhängig abschließbar:** Ja, eigener Cleanup mit dokumentiertem Handoff.
- **Vorgeschlagener Workstream:** Ungruppiert, querschnittliche Dokumentmigration.

## 6. Zur Planungszeit vorgeschlagene Workstreams

Diese Gruppierung dokumentiert die damalige Empfehlung, nicht angelegte Projects oder aktuelle Boardstruktur. Die damalige Empfehlung lautete, bestehende passende Board Projects bevorzugt wiederzuverwenden.

| Kohärenter Workstream | Tasks | Gemeinsames Ergebnis |
|---|---|---|
| Plattform- und Bedienabnahme | H02, H03, H04 | Native Plattformberichte und reale Admin-Bedienmatrix |
| Verbundener MCP-/Orchestratorbetrieb | H01, H07, L07 | Echter Provider-Round-Trip, installierte Beilage und separat beobachteter Skillworkflow |
| Getrennter Betrieb und Sicherheitsabnahme | H06, H05 | Technische Isolationsevidenz plus unabhängiges konkretes Sicherheitsurteil |
| Review-Bedienanforderungen | L03, L04, L05, L06, L08 | Erhaltene kleine Bedienanforderungen ohne neue Produktarchitektur |
| Kein neues Projekt nötig | L01, L02, L09 | Kleine/querschnittliche Repository- und Dokumentkonsistenz |

Die folgende Reihenfolge ist die Empfehlung zum Zeitpunkt der ursprünglichen Planung, kein aktueller Arbeitsplan: L01→L02 für reproduzierbare Einstiegspfade; H02/H03 parallel nach verfügbaren Geräten; H04 nach mindestens einem Plattformnachweis. H01 und H07 können mit ihren eigenen Voraussetzungen unabhängig erfolgen. H06 liefert Isolationsbefunde, H05 folgt als unabhängige Bewertung. L03–L08 sind separat abschließbar. Das Taskboard bestimmt ausschließlich die tatsächliche Ausführungsreihenfolge.

## 7. Vollständige historische Status-Inhaltszuordnung

Die Zeilennummern in dieser Zuordnung beziehen sich auf den ursprünglichen, 38-zeiligen Haupttext der `IMPLEMENTATION_STATUS.md` vor dem jetzt vorgeschalteten historischen Hinweis. Der hinzugefügte Hinweis verschiebt die aktuellen Dateizeilen um zwei; der darunterliegende Statusinhalt blieb erhalten. SHA-256 des Statusdokuments vor der Dokumentmigration: `c5b04a7d0e76540efc4728a2ae48e3494fe132a891ba34986fa457f4734610a8`.

| Statusinhalt | Dauerhafter Konzept-/Evidenzhome | Damalige Zuordnung zu Taskkandidaten |
|---|---|---|
| Titel/Datum/0.8, Ein-Datei-Controller, separate nicht importierende Bridge, keine Veröffentlichung (1–3) | Konzept Einleitung, §§3–4,19.1–19.2 | Keine offene Implementierungspflicht; kein Publikationsauftrag |
| PTY/Terminal/IPC, Linuxnachweis und offenes macOS (7) | §§4–6,17,19.1; vorhandene Testberichte | H02 für macOS, keine zweite PTY-Implementierung |
| GUI/Review/Scrollen, separates Terminalfenster (8) | §§2,5,7 | Bereits implementiert; kleine erhaltene Bedienlücken nur L03–L06/L08 |
| Command-/Outputfreigabe, Return, SINGLE–LARGE (9) | §§7–8,10,14 | Bereits implementiert; kein neuer Basisfreigabe-Task |
| HTTP/Long-Poll/Netzclient, Schemata/Limits/Backoff (10) | §§11–12 | Bereits implementiert |
| Vier MCP-Tools, expliziter Chat-Code, keine Ausführungs-/Dateifähigkeiten (11) | §12, insbesondere Tooltabelle/Bearer-Bindung | Bereits implementiert; echter Providerbeleg ausschließlich H01 |
| Offizielles Tunnelbinary v0.0.15, Start/Health/Stop, lokaler Round Trip, kein Download (12) | §12.1,19.1 | Bereits implementiert; echter Providerbeleg ausschließlich H01 |
| TLS-SPKI/Pairing/Revocation, lokale Tests, menschlicher Vergleich (13) | §13 | Menschlicher Vergleich ist Betriebsregel, keine neue API; Isolationprüfung H06 |
| Private atomare Speicherung, keine wiederhergestellten Freigaben (14) | §§14–15 | Bereits implementiert |
| Empfänger-/Body-/Idempotenzgebundener Output-Retry, 24 Stunden (15) | §§14–15 | Bereits implementiert |
| Streaming-Sanitizer, kein Vollterminalrenderer (16) | §§10–11,16 | Bereits implementiert; Renderer bleibt ausgeschlossen |
| Headless-Kern, 49+36+1 historische Tests, Standardbibliotheksframework (17) | §§4,19.1; `tests/MVP_0.8_TEST_REPORT.md` und Logs | Historisch geprüft; Reorganisationsreparatur nur L01 |
| Skill erstellt/validiert, persönliche Speicherung blockiert (18) | §12.1 einschließlich Handoff-Provenienz | H07 Installation; L07 betrifft ausschließlich den zusätzlichen Live-Nachweis der neu rekonstruierten Beilage |
| Onboarding beschrieben/lokal getestet, echte Konfiguration erforderlich (19) | §§12.1,17,19.1 | H01 |
| Qubes/Gateway-Architektur, Remote technisch, keine Hardwareabnahme (20) | §§15,17,19.1 | H06 |
| Echte ChatGPT-Verbindung mit Tunnel-ID/Runtime-Key/Workspace (24) | §§12.1,17 | H01 |
| Native macOS-/Terminal.app- und praktische Debian-Abnahme; Ubuntu-Prüfumgebung (25) | §§5,17,19.1 | H02 macOS; H03 Debian – getrennte Teilpflichten, keine Duplikate |
| Reale SSH-/sudo-/TUI-Probe und konkreter Sicherheitsreview (26) | §§6,8–10,16–17 | H04 Bedienprobe; H05 unabhängiger Review – getrennte Ergebnisse |
| Isolation: Firewall, Dateien, Agenten, Clipboard (27) | §§15–17 | H06 |
| Skill nach ausdrücklicher Zustimmung dauerhaft installieren (28) | §§12.1,17 | H07 |
| Keine erfundenen Prüfergebnisse; Risk/Secrets, exakte Grenzen/Exitcodes, eingebetteter Emulator ausgeschlossen (30) | Leseregel, §§1,3,9–10,16,19.1 | Ausschlüsse, keine Tasks |
| 86 Tests am 30.09.2026, Ubuntu/Linux/Python, realer Client gegen lokalen Simulator, keine echte Cloud, Readiness lokal (34) | §§12.1,19.1; historischer Testbericht | Cloud-Lücke H01; übriger Inhalt Evidenz |
| Same-UID-Dateisystemgrenze, Bearer statt Provider-Chat-ID, Beobachtungsmarker, Risk/Redaction/Renderer/Qubes-RPC ausgeschlossen (36) | §§1,3,10,12,15–16 | H05/H06 für unterschiedliche Review-/Isolationspflichten; keine Ausschluss-Tasks |
| Standardbibliothek nur lokaler Controller/Tests, aiohttp/cryptography für Netzwerk/TLS, kein Testframework-/Agentenstack (38) | §§5,12,19.1 | Bestehende Voraussetzungen; praktische Debian-Kombination H03 |

Überschriften/Leerzeilen (5–6,21–23,29,31–33,35,37) führen keine zusätzliche Pflicht ein. Alle materiellen Statusaussagen sind oben zugeordnet. Quellenkontinuität der vier Python-Dateien ist per SHA-256 mit dem historischen Bericht abgeglichen; keine Tests wurden in dieser Migration ausgeführt.

## 8. Migrationsnachweis und Archivierungsstatus

- [x] **Konzeptinhalt synthetisiert:** Jede materielle Statusaussage besitzt den in §7 genannten dauerhaften Konzept-/Evidenzhome. Das Konzept integriert den damaligen Stand thematisch statt als Changelog und trennt vorhandene Quelle, historische Evidenz, Soll und Ausschluss.
- [x] **Jede offene Statuspflicht hat genau einen Board-Home:** H01–H07 decken Zeilen 24–28 und ihre Tabellenwiederholungen ab. Plattformen und Bedienprobe/Review sind nach eigenem Ergebnis getrennt. L01–L09 sind den in §2 festgehaltenen Tickets zugeordnet.
- [x] **Keine einzigartige erforderliche Information bleibt nur im Status:** Architektur, Implementierungsstand, Plattform-/Cloudgrenzen, Skill-Provenienz, Testzahlen/-umgebung, Sicherheitsgrenzen, Ausschlüsse und Abhängigkeiten stehen im Konzept beziehungsweise vorhandenen Testnachweisen. Historische Zahlen werden nicht zu neuen Prüfergebnissen umgedeutet.
- [x] **Board-Referenzen festgehalten:** Operator hat H01–H07 und L01–L09 als bestehende/verifizierte Abdeckung gemeldet; die IDs sind in §2 zugeordnet. Die zwei späteren aktiven Tickets sind dort als Referenzen ergänzt.
- [x] **Aktive Arbeit auf Taskboard verwiesen:** Konzept, Statusdatei und dieser Vorschlag erklären die ausschließliche Board-Zuständigkeit. Diese Datei und der Status bleiben historische/evidence references, keine parallele Todo-Liste.

**Verdikt:** Der Planungs-/Taskboard-Handoff ist abgeschlossen. `IMPLEMENTATION_STATUS.md` wird auf ausdrückliche Vorgabe beibehalten und dient nur als historischer Stand/Evidenz. Ein späterer Löschschritt ist von diesem Handoff nicht umfasst und benötigt eine eigene Autorisierung; offene Arbeit wird ausschließlich anhand der Taskboard-Tickets gesteuert.

## 9. Entscheidungen und bewusst nicht erzeugte Tasks zur Planungszeit

**Kein INPUT_REQUIRED für diese Synthese.** Die maßgebliche Revision, MVP-Verfeinerungen und Ausschlüsse waren aus Dokumenten/Quelle bestimmbar. Der dokumentierte Return-Erststart „an“, separates Systemterminal, saubere Bash, ein lokaler Vorschlagsplatz, vier MCP-Tools und Bearer-Chatbindung wurden weder zurückgedreht noch als größere Sicherheitszusage ausgelegt.

Eigene Cloud-Credentials/Workspace, macOS-/Debian-/Qubes-Prüfgeräte, unabhängige Reviewperson und Zustimmung/Zielort für H07 müssen bei der jeweiligen Ausführung verfügbar sein. Diese Voraussetzungen machen die Planung nicht zu einer ungeklärten Geschäftsentscheidung. Ein tatsächlicher negativer Plattformbefund kann später eine Backendentscheidung erforderlich machen; sie wird hier nicht erfunden.

Zum Zeitpunkt der Synthese wurden keine spekulativen Tasks für eingebettetes Terminal, Risk-/Secret-Erkennung, exakte Exitcodes, Qubes-RPC, mTLS, zusätzliche REST-GETs, Shellprofilübernahme, Installer, CI, Paketframework, automatisches Fensterlayout oder Veröffentlichung vorgeschlagen. Spätere zusätzliche aktive Arbeit ist nur durch die zwei Ticketreferenzen in §2 dokumentiert; aktuelle Abgrenzung und Boardstatus liegen in diesen Tickets. Der unversionierte Import wurde als Repository-Zustand dokumentiert, aber nicht als Commit-/Remote-Auftrag abgeleitet.
