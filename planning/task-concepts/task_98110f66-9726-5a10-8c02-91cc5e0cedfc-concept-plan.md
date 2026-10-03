Task-ID: task_98110f66-9726-5a10-8c02-91cc5e0cedfc
Title: HTTP-/Event-Layer konsolidieren: aiohttp-Ablösung und approved-output Continuation
Status: implemented (Board bleibt im Orchestrator-Move `in_progress` bis zur endgültigen Abnahme)
Last-concept-update: 2026-10-03T02:13:00Z

Kompaktkontext: .opencode/tasks/task-task_98110f66-9726-5a10-8c02-91cc5e0cedfc.compact.md

Dieses Dokument ist die kanonische Architektur-, Anforderungs- und Testwahrheit dieses Tasks. Board-Beschreibung und alle vier Management Notes (Push+Pull-Zielsemantik, Skill-/Agenten-Loop, OFFICIAL-DOC-UPDATE MCP Events/Webhook, Attention-Queue-Korrektur) sind vollständig eingearbeitet. Überholte frühere Aussagen sind in §12 vermerkt.

## 1. Ziel und Scope

Der Task konsolidiert zwei eng gekoppelte Themen:

1. aiohttp-Ablösung: die externe Python-Runtime-Abhängigkeit aiohttp entfernen, ohne Funktions-, Sicherheits- oder Wartungsabstriche. Wäre das nicht vollständig möglich, wäre die kleinste verbleibende Laufzeitabhängigkeit konkret zu begründen. Aktueller Stand: die vollständige Ablösung ist machbar (siehe §4).
2. Approved-output Continuation: nach menschlicher Output-Freigabe den Orchestrierungsloop über MCP-Events-basierten Push fortsetzen, soweit die Host-Surface das unterstützt, mit Pull als jederzeit robustem Fallback und einem einzigen kanonischen Delivery-State.

Nicht im Scope: Attention Queue (ausdrücklich außerhalb), kein Approval-Bypass, keine zweite konkurrierende Transportarchitektur, keine End-to-End-Wakeup-Aussage ohne praktische Workspace-Evidence, keine Abschwächung von Auth/TLS/Pairing/Conversation-Binding.

## 2. Verifizierter Ausgangszustand (Code-Evidenz 2026-10-03)

Audit am Produktcode für admin_in_the_loop.py bei HEAD 7a20745e7d61b508d0fef85d1535b56ff9e79be2. Alle aiohttp-Nutzung ist serverseitig im Embedded-Bridge-Block (4 Importstellen):

| Aspekt | Ist-Code | Zeilen |
|---|---|---|
| HTTP-Server | web.Application mit Guard-Middleware (Host/Origin, Dubletten-Header, Rate-Limit 429, Inflight unter 64, Body-Limits) | 5234–5263 |
| HTTP-Client | kein aiohttp; der Controller nutzt stdlib http.client (BridgeHTTP) inklusive TLS-Pinning | 1540–1621 |
| Long-Poll | asyncio.Event mit GET /v1/inbox?wait (maximal 25 s) | 5201, 5275–5295 |
| MCP-Endpoint | POST /mcp, JSON-Antworten, HTTP 202 für Notifications | 5323–5421 |
| TLS | ssl.SSLContext aus bridge_certificate, angebunden über TCPSite | 5424–5485 |
| Shutdown | Signal-Handler, AppRunner.cleanup, ready_fd-Bootstrap | 5472–5581 |
| Dependency-Check | import-aiohttp-Guard ("aiohttp_package_required") | 2277–2282 |

Tools: submit_proposal, get_submission_status, get_approved_output, get_preferences (5175–5188). Protokollversionen: 2026-07-28, 2025-11-25, 2025-06-18, 2025-03-26 (4845). Der Controller bindet Output-Freigabe an last_proposal_id (letzte Transfer-Heuristik, approve() um Zeile 2968), was bei Multi-Flight-Submission unzureichend ist (§5).

Ältere, pauschale aiohttp-Nutzungsangaben sind damit überholt; das obige Audit definiert exakt die zu ersetzenden Stellen.

## 3. Push-Pfad: MCP Events über Webhooks (primär)

Verankert an offizielle OpenAI-Doku (https://developers.openai.com/plugins/build/mcp-events, abgerufen 2026-10-03). ChatGPT unterstützt für MCP Events ausschließlich Webhook-Delivery; die ChatGPT-Integration erlaubt für Events kein Polling und kein Streaming. Deshalb bauen wir weder für Polling noch für Streaming eine eigene zweite Infrastruktur.

Voraussetzung für die Subscriptions-API am MCP-Endpoint:

- "events" wird nur in der modernen Variante in server/discover advertised, mit capabilities.events = {}; legacy-Versionen zeigen unverändert nur tools und antworten auf die Events-Methoden mit -32601.
- Neuer Methodensatz am selben Endpoint: events/list, events/subscribe, events/unsubscribe.
- Im Scope des ersten Skizzenumfangs genau ein Event-Typ: Name = "approved_output.available". Basisdefinition: delivery = ["webhook"], inputSchema mit optionalem Filter submission_id, payloadSchema mit Pflichtfeldern submission_id, output_id, expires_at.
- Subscribe: ChatGPT übergibt delivery: {url, secret}; Secret mit whsec_ Präfix und base64 von 24 bis 64 Randombytes. Server prüft HTTPS only, öffentliche IP (kein Loopback, kein RFC1918), keine Redirects, const-time-Signaturvergleich bei der Callback-Verifikation.
- Callback-Verifikation: einmalig je Subscription-URL; signierte Challenge nach Standard Webhooks (webhook-id, webhook-timestamp, webhook-signature, X-MCP-Subscription-Id); ein 2xx mit Challenge-Echo aktiviert Delivery; sonst -32015 mit kategorisiertem Grund (challenge_failed, timeout, http_4xx, http_5xx, connection_refused, tls_error).
- Subscription-Store in der bestehenden SQLite-Bridge-Datenbank: deterministische ID aus (AI-Principal, callback URL, Event-Name, kanonisch normalisierte Filterargumente); persistente Felder URL, Secret, Filter, refreshBefore; TTL-Default 24 h; refresh via erneutes events/subscribe vor refreshBefore. Subscriptions überleben einen Bridge-Restart. Unsubscribe ist idempotent.

## 4. HTTP-Layer: stdlib-only Neubau (aiohttp-Ersatz)

Decision: aiohttp wird vollständig entfernt. Der HTTP-Server wird mit der Standardbibliothek neu gebaut; konkret: ThreadingHTTPServer mit angepasstem Handler, plus ssl.SSLContext. Standard-Webhooks-Signierung über stdlib hmac/hashlib; keine zusätzliche externe Abhängigkeit.

Begründung:

1. Die genutzten Features sind schmal (dual-port HTTP listener, Guards, Body-Limits, Response-Framing, 202, TLS-Site, Shutdown) und mit stdlib reproduzierbar.
2. Der Controller-Client ist bereits stdlib; eine zweite Implementierung entfällt.
3. h11 wäre eine neue externe Abhängigkeit ohne Vorteil; stdlib ist die einfachste Variante.
4. Keine Streaming- oder Multipart-Anforderungen; das Verhalten des Bridge-Servers wird in Paritätstests gegen das heutige Verhalten geprüft.

Paritätspflichten des stdlib-Servers:

- Routen unverändert: POST /mcp (JSON, 202 für Notifications), GET /v1/status, GET /v1/inbox?wait (max 25 s, long-poll via threading.Event), POST /v1/approved-output, POST /v1/pair/claim, POST /v1/pair/confirm, PUT /v1/preferences.
- /mcp akzeptiert nur application/json; Content-Encoding-Fehler wie heute; Body-Limit 128 KiB; Timeout-Ersatz für asyncio.timeout(5) über den Request-Socket-Timeout.
- Request-Wächter 1:1: Origin-403, Host-Whitelist 403, URL-Längenlimit, Dubletten-Header-Abweisung, globale und Per-Token-Rate-Limits (429/503), Inflight-Grenze (Semaphore); keine Logausgaben mit Payloads.
- Body-Lesen nur aus Content-Length; fehlende Content-Length oder Transfer-Encoding: chunked → 411 bzw. 400.
- SQLite-Zugriffe im Request-Thread, gleich wie heute; Thread isolation per threading-Semaphor.
- Zwei Sockets über gemeinsamen BridgeStore; MCP-Socket bleibt immer Loopback; Controller-Socket behält die heutige TLS-Regel.
- Signal-Handling und saubere Shutdown-Pfade bleiben; ready_fd-Bootstrap exakt gleich.

## 5. Controller-Korrelation (Multi-Flight-tauglich)

Defizit heute: die Output-Freigabe bindet an last_proposal_id (letzte Transfer-Heuristik, ohne sichtbare Bindungsanzeige für den Menschen); das ist unzureichend bei mehreren parallelen Submissions.

Ziel:

- Das TO_OUTPUT_SINK-Review zeigt die Kandidaten (nicht abgelaufene Submissions dieses Empfängers) und einen Bindingsslot: "an submission X gebunden" oder "unbound". Default bleibt die letzte Transfer-Vorgabe (single-flight kompatibel), aber der Mensch sieht und wählt die Bindung explizit, bevor Approve ausgelöst wird.
- Der Review bleibt nach Approval unveränderbar; kein zweites Binden.
- unbound (submission_id=None) bleibt möglich, wie heute von der Bridge-API abgedeckt.
- Der Controller speichert aktiv die Submission-IDs je Verbindung (kein last-Marking mehr nötig); die gewählte Bindung fließt direkt in die approve-Payload (submission_id als explizites Feld).

Der Umfang bleibt klein; Ausführung in §8, Schritt 4.

## 6. Kanonischer Delivery-State

Storage: bestehende Tabelle outputs in bridge.sqlite3, erweitert um delivered (INTEGER 0/1) und delivered_at (nullable REAL). Es entsteht keine zweite Queue-Tabelle; Pull und Push greifen auf dieselbe Zeile zu.

Regeln:

1. delivered=1 wird nur nach bestätigtem 2xx-Webhook-Ack gesetzt (belastbar bestätigte Zustellung).
2. Ein bloßer Push-Versuch ohne Ack ändert nichts am Output-State; delivered bleibt 0; die Nutzbarkeit über Pull bleibt unverändert.
3. Idempotenz: Schlüssel ist output_id (Event-ID). Mehrfache 2xx-Acks (Retry, Doppel-Delivery) sind nach erstmaligem Setzen idempotent.
4. get_submission_status (submission_id): nach 2xx-Ack ist approved_output_available=false. Das verhindert, dass der Pull-Pfad dieselbe Nachricht erneut als neue ausstehende Nachricht liefert.
5. get_approved_output (mit expliziter submission_id): bleibt innerhalb des 24-h-Retentionsfensters lesbar (die Autorisierung bleibt identisch); das ist der für den Skill-Workflow relevante Readback-Pfad.
6. get_approved_output ohne submission_id-Filter (kursorbasierter generischer Pull): bereits delivered Outputs werden standardmäßig übersprungen; sie werden nicht erneut als neue Nachricht ausgeliefert.
7. Pull bleibt jederzeit robuster Fallback, besonders dann, wenn Events nicht unterstützt sind, ihr Delivery nicht bestätigt wird oder eine Subscription abgelaufen ist.

## 7. Security-Grenzen (nicht verhandelbar)

1. Kein Event vor der menschlichen Output-Freigabe; Emissions-Quelle bleibt exklusiv der approved-output-Upload.
2. Keine Terminaldaten, kein Conversation-Code, keine Tokens oder Secrets in Event- und Webhook-Pfaden; keine Logausgaben mit Payload-Exposition.
3. Auth, TLS, Pairing und Conversation-Binding bleiben unverändert; der Webhook-Versand führt die strengen SSRF-Checks (nur HTTPS, nur öffentliche Ziele, const-time-Verifikation, keine Redirects) ohne jede Erweiterung der bestehenden Auth-Regeln.
4. Skalen-Grenzen: Subscriptions sind begrenzt und klar gekapselt; der Subscription-Store nutzt die bestehende SQLite-Technik.
5. Legacy-Protokollpfade bleiben intakt; keine Änderung an bestehenden Tool-Schemata.
6. Der Tunnel-Pfad bleibt unverändert; kein neuer externer Listener.

## 8. Umsetzungssequenz (jeder Schritt mit abschließender Paritätstestfolge)

1. Schritt 1: stdlib-HTTP-Layer implementieren in admin_in_the_loop.py, alle vier aiohttp-Importstellen ersetzen; CLI-Verhalten unverändert; DEPENDENCY_CHECK entfällt; Paritätstests aktualisiert in tests/test_connected_mvp.py.
2. Schritt 2: events/list, events/subscribe, events/unsubscribe implementieren (nur MCP 2026-07-28); Subscription-Store in SQLite (persistent); TTL- und Refresh-Logik.
3. Schritt 3: Webhook-Delivery und Callback-Verifikation: signierte POSTs via hmac, SSRF-Guards, Retry-Backoff, delivered-Ack-Pfad, 410/413-Regeln.
4. Schritt 4: Delivery-State in outputs plus Pull-Filterung per get_submission_status und get_approved_output; Controller-Migration zur expliziten Binding; keine zweite Queue.
5. Schritt 5: Skill-Workflow-Dokumentation korrigieren (Korrelation, Branch-Logik, kein Auto-Exec); README/IMPLEMENTATION_STATUS/Local_Integration_README anpassen (aiohttp-Entfernung). Konzept-Doku-Verweise im Board.

Testumgebung: bestehende unittest-Suites erweitert; keine neuen Frameworks. Testdatei bleibt tests/test_connected_mvp.py.

## 9. Testmatrix (Board-Acceptance-Criteria abgedeckt)

1. Pull-only Erfolg: alle bestehenden get_submission_status/get_approved_output-Fälle unverändert grün; keine Pull-Regression.
2. Events-Discovery: capabilities.events nur in 2026-07-28; legacy-Nutzung ohne Events; subscription-Registration korrekt.
3. Callback-Verifikation: Erfolgs-Szenario (signierte Challenge) und Fail-Szenarien (unmatched Signature, Nicht-HTTPS, private IP, Redirect, Timeout) jeweils -32015 mit Kategorie.
4. SSRF-Abweisung: private/Loopback-Adresse abgelehnt; kein Credentials-Leak; keine Redirects.
5. Emission: Event nur nach gültiger menschlicher Output-Freigabe; keines bei proposal-Anforderung; legacy-Mode ohne Events.
6. Payload-Regeln: kein Output-Text, kein Conversation-Code, keine Secrets; payloadSchema eingehalten.
7. Delivered-Idempotenz: doppelte eventId-Deliveries/2xx-Acks verändern nichts zusätzlich; keine doppelte Business-Semantics; Receiver dedup via eventId; Store-Werte nur via output_id änderbar.
8. Pull nach 2xx-Ack: get_submission_status False; get_approved_output mit exakter submission_id bis zum Retentionsschluss lesbar; genereller Pull überspringt delivered Outputs.
9. Push-Failure: Webhook-Timing/TCP-Fehlschlag/5xx leaves delivered=0; Pull-Verfügigkeit ungestört; Retry-Backoff dokumentiert; 410/413 non-retryable.
10. Restart-Verhalten: Subscriptions persistieren via SQLite; die delivered-Spalten bleiben persistent.
11. Multi-Flight-Korrelation: der Controller hält zwei gestackte Submissions korreliert via Output-Binding; der Skill liest je exakte submission_id; keine last-command-Heuristik; kein doppeltes Command-Dispatch bei doppelter Delivery.
12. Branch-Logik (Skill-Doku): goal-complete / next-command / user-decision / diagnose konsistent dokumentiert; jeder neue Command bleibt approval-getrieben.
13. aiohttp-freie Runtime: --bridge startet ohne aiohttp-Installation; Meldungstexte aktualisiert.
14. README/Doc-Fidelity: Dependency-Checks, Installationshinweise und Konzepttexte auf stdlib-only aktualisiert; konsistent über Releases.

## 10. Workspace-/E2E-Abnahme (außerhalb des Repos)

Die End-to-End-Verifikation im echten ChatGPT-Work-Workspace (Event-Continuation ohne zusätzlichen User-Turn) bleibt außerhalb dieses Implementierungsumfangs und ist Operator-Sache (separates Board-Ticket). Der serverseitige Flow ist lokal vollständig automatisiert testbar.

## 11. Portabilität / Kompatibilität (macOS + Debian)

- Python >=3.11 (POSIX-only), Debian 12/13 und macOS. Runtime: nur Python-Standardbibliothek plus cryptography. aiohttp entfällt final.
- Installationshinweise (README, IMPLEMENTATION_STATUS, tests/Local_Integration_README): Hinweise auf python3-aiohttp entfallen.
- Deployment: kein extra Container, keine Zusatzprozesse; die beiden neuen SQLite-Spalten benötigen kein separates Migrationstool.
- Breaking-Change-Surface: nur die zusätzliche Events-API (modern-only) und die delivered-Semantik; bestehende Tool-Contracts bleiben unverändert. Die Subscriptions-API ist nachträglich additiv erweiterbar.

## 12. Risiken / Annahmen / überholte Aussagen

1. MCP Events ist Design-Sketch-Niveau (experimental, kein Major-Spec-Release). Der Web-Handler ist bewusst klein: nur list/subscribe/unsubscribe plus Webhook-Send. Semantikänderungen der Extension werden additiv adaptiert; Pull-Pfad und Auth-Regeln bleiben unabhängig.
2. Die reale Workspace-Verfügbarkeit (Bitsurance ChatGPT-Work) ist praktische Abnahme auf Operator-Seite (separates Abnahme-Ticket); das ist kein Architekturblocker, sondern eine offene Acceptance-Grenze.
3. Die frühere Note "automatisches ChatGPT-Wakeup in offiziellen Quellen nicht belegt" ist überholt. Die offizielle OpenAI-Doku (abgerufen 2026-10-03) dokumentiert event-triggered Work-Flows mit MCP Events. Es bleibt ausdrücklich die Regel: kein End-to-End-Wakeup-Claim ohne echte Workspace-Evidence.
4. Falls ein Deployment-Kontext aiohttp explizit vorhanden hält, bleibt der Code stdlib-only; aiohttp wird nicht mehr benutzt und die Importstellen werden endgültig entfernt.
5. Attention Queue bleibt außerhalb des Scopes (Board-Korrektur vom 2026-10-02); kein zusätzliches Board-Ticket.
6. Es gibt keine automatische Approval- oder automatische Folgeausführung; jedes Command bleibt vollständig an die menschliche Approval-Semantik gebunden.


## 13. Implementierungsnachweis (2026-10-03)

Umgesetzter Code und Tests anhand dieses Plans:

- `admin_in_the_loop.py`: der Embedded-Bridge-Block arbeitet vollständig auf der Standardbibliothek. `BridgeService` bedient POST `/mcp`, `/v1/*` (Controller) mit 1:1-Guards (Origin/Host/Dupletten-Header/Limits/Rate/Inflight), 202-Notifications, threading-Long-Poll; `BridgeStore` erhält die `subscriptions`-Tabelle und die Outputs-Spalten `delivered`/`delivered_at`; `events/list|subscribe|unsubscribe` werden nur in der modernen Variante advertiseiert; Standard-Webhooks-Signierung mit stdlib hmac; Callback-Verifikation, SSRF-Prüfung (nur HTTPS, nur öffentliche Ziele, fixed destination, keine Redirects); Retry mit 3 Versuchen; 2xx-Ack markiert `delivered`. `AppCore` speichert transferred Submissions; `approve()` nimmt eine explizite `submission_id`; die Review-UI zeigt den Bindingsslot.
- `tests/test_connected_mvp.py`: zusätzliche Klassen `EventSubscriptionTests` und `EventDeliveryTests` mit In-Process-`ServiceHarness`; Abdeckung von Discovery, Subscribe/unsubscribe/Refresh/Idempotenz, Validation-Fehlern (-32602/-32011/-32013/-32015), Challenge/Mismatch, Standard-Webhooks-Signatur, SSRF-Adress-Guards, Event-Nur-nach-Approval, Payload-Shape, Filter-Korrelation, Pull-Fallback bei nicht bestätigtem Zustellversuch, Delivery-Idempotenz mit gleichem eventId/Submissions-Retry, Restart-Persistenz und multi-flight genauer Binding im `ConnectedRoundTripTests`.
- `skill/SKILL.md`: Event-Continuation-Abschnitt (Plan §6.3). README/IMPLEMENTATION_STATUS/Local_Integration_README ohne aiohttp; historische Konzept-Dokumente bleiben unverändert.

Testnachweis der lokalen Regression (2026-10-03):

1. `python3 admin_in_the_loop.py --self-test` → 49/49 OK.
2. `python3 tests/test_connected_mvp.py` → 55 Tests, OK (2 skips, offizielles Tunnel-Binary nicht im Environment).
3. `xvfb-run -a python3 tests/gui_smoke_test.py` → 16/16 OK unter Xvfb.
4. `import admin_in_the_loop` ohne aiohttp-Installation; keine aiohttp-Referenz mehr im Bridge-/Test-/Skill-Kontext.

Offen: Der praktische E2E-Nachweis im echten ChatGPT-Work-Workspace (Event-Continuation ohne zusätzlichen User-Turn) ist weiterhin ein separates Operator-Abnahme-Ticket und nicht Teil dieser Tests. Kein Release-/Deployment-Schritt wurde durchgeführt.
