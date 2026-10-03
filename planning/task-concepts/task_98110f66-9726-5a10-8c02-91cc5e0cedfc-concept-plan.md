Task-ID: task_98110f66-9726-5a10-8c02-91cc5e0cedfc
Title: HTTP-/Event-Layer konsolidieren: aiohttp-Ablösung und approved-output Continuation
Status: REVIEW_PASS; lokaler Connected-Continuation-Pfad integriert, externer Host-Schritt PENDING_EXTERNAL (Board bleibt `in_progress`)
Last-concept-update: 2026-10-03T22:39:55Z

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
- Genau ein Event-Typ: Name = "approved_output.available". Basisdefinition: delivery = ["webhook"], inputSchema mit erforderlichem menschlich übergebenem `conversation`-Code und optionalem Filter submission_id, payloadSchema mit Pflichtfeldern submission_id, output_id, expires_at. Subscribe, Refresh/Secret-Update und passendes Unsubscribe erfordern `arguments.conversation`; der Code wird nicht als Filter persistiert und nie als Event-Payload versandt.
- Subscribe: ChatGPT übergibt delivery: {url, secret}; Secret mit whsec_ Präfix und base64 von 24 bis 64 Randombytes. Server prüft HTTPS only, öffentliche IP (kein Loopback, kein RFC1918), keine Redirects, const-time-Signaturvergleich bei der Callback-Verifikation.
- Callback-Verifikation: einmalig je Subscription-URL; signierte Challenge nach Standard Webhooks (webhook-id, webhook-timestamp, webhook-signature, X-MCP-Subscription-Id); ein 2xx mit Challenge-Echo aktiviert Delivery; sonst -32015 mit kategorisiertem Grund (challenge_failed, timeout, http_4xx, http_5xx, connection_refused, tls_error).
- Subscription-Store in der bestehenden SQLite-Bridge-Datenbank: deterministische ID aus (AI-Principal, callback URL, Event-Name, kanonisch normalisierte Filterargumente ohne Code); persistente Felder URL, Secret, Filter, refreshBefore und `bound`-Autorisierungsmarker; TTL-Default 24 h; refresh via erneutes events/subscribe vor refreshBefore. Subscriptions überleben einen Bridge-Restart. Historische Zeilen ohne Binding-Prüfung benötigen einen autorisierten Refresh, bevor Delivery wieder aktiv ist. Unsubscribe ist mit gültiger Autorisierung idempotent.

## 4. HTTP-Layer: stdlib-only Neubau (aiohttp-Ersatz)

Decision: aiohttp wird vollständig entfernt. Der HTTP-Server wird mit der Standardbibliothek neu gebaut; konkret: ThreadingHTTPServer mit angepasstem Handler, plus ssl.SSLContext. Standard-Webhooks-Signierung über stdlib hmac/hashlib; keine zusätzliche externe Abhängigkeit.

Begründung:

1. Die genutzten Features sind schmal (dual-port HTTP listener, Guards, Body-Limits, Response-Framing, 202, TLS-Site, Shutdown) und mit stdlib reproduzierbar.
2. Der Controller-Client ist bereits stdlib; eine zweite Implementierung entfällt.
3. h11 wäre eine neue externe Abhängigkeit ohne Vorteil; stdlib ist die einfachste Variante.
4. Keine Streaming- oder Multipart-Anforderungen; das Verhalten des Bridge-Servers wird in Paritätstests gegen das heutige Verhalten geprüft.

Paritätspflichten des stdlib-Servers:

- Routen unverändert: POST /mcp (JSON, 202 für Notifications), GET /v1/status, GET /v1/inbox?wait (max 25 s, long-poll via threading.Event), POST /v1/approved-output, POST /v1/pair/claim, POST /v1/pair/confirm, PUT /v1/preferences.
- /mcp akzeptiert nur application/json; Content-Encoding-Fehler wie heute; Body-Limit 128 KiB; monotone 5-s-Gesamtfrist für TLS/Request-Line/Headers/Body mit kontrollierten `read1`-Schritten statt bloßem Socket-Inactivity-Timeout. Long-Poll bleibt davon unverkürzt (max. 25 s nach vollständig gelesener Anfrage).
- Request-Wächter: Origin-403, Host-Whitelist 403, URL-Längenlimit, Dubletten-Header-Abweisung, globale und Per-Token-Rate-Limits (429/503), 64-Connection-Budget gemeinsam über beide Listener vor Thread-Erzeugung plus bestehende Inflight-Grenze; keine Logausgaben mit Payloads. Überzählige Connections werden geschlossen; Slots werden auch auf Fehler-/Timeoutpfaden freigegeben.
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

1. delivered=1 wird nur nach bestätigtem 2xx-Webhook-Ack und erneuter, atomarer Autorisierungsprüfung der human-bound Subscription und ihres AI-Principals gesetzt. Revocation/Unsubscribe/Expiry vor ACK suppressen keinen Pull-Fallback. Ein bereits gestarteter Send kann nicht zurückgerufen werden.
2. Ein bloßer Push-Versuch ohne Ack ändert nichts am Output-State; delivered bleibt 0; die Nutzbarkeit über Pull bleibt unverändert.
3. Idempotenz: Schlüssel ist output_id (Event-ID). Mehrfache 2xx-Acks (Retry, Doppel-Delivery) sind nach erstmaligem Setzen idempotent.
4. get_submission_status (submission_id): nach 2xx-Ack ist approved_output_available=false. Das verhindert, dass der Pull-Pfad dieselbe Nachricht erneut als neue ausstehende Nachricht liefert.
5. get_approved_output (mit expliziter submission_id): bleibt innerhalb des 24-h-Retentionsfensters lesbar (die Autorisierung bleibt identisch); das ist der für den Skill-Workflow relevante Readback-Pfad.
6. get_approved_output ohne submission_id-Filter (kursorbasierter generischer Pull): bereits delivered **bound** Outputs werden standardmäßig übersprungen; unbound Outputs bleiben darüber lesbar, weil sie keinen benannten Submission-Readback besitzen.
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

## 10. Workspace-/E2E-Abnahme (Connected-Lauf)

Die echte End-to-End-Verifikation im ChatGPT-Work-Workspace (Event-Continuation ohne zusätzlichen User-Turn) ist ein externer Schritt innerhalb des kanonischen Connected-/B2-Akzeptanzlaufs; sie ist in §16 festgehalten. Operatorentscheidung vom 2026-10-03: kein separater Wakeup-Sonderlauf. Lokale Tests belegen nur den deterministisch ausführbaren Server-/Controller-Pfad, nicht die reale Host-Continuation.

## 11. Portabilität / Kompatibilität (macOS + Debian)

- Python >=3.11 (POSIX-only), Debian 12/13 und macOS. Runtime: nur Python-Standardbibliothek plus cryptography. aiohttp entfällt final.
- Installationshinweise (README, IMPLEMENTATION_STATUS, tests/Local_Integration_README): Hinweise auf python3-aiohttp entfallen.
- Deployment: kein extra Container, keine Zusatzprozesse; die beiden neuen SQLite-Spalten benötigen kein separates Migrationstool.
- Breaking-Change-Surface: nur die zusätzliche Events-API (modern-only) und die delivered-Semantik; bestehende Tool-Contracts bleiben unverändert. Die Subscriptions-API ist nachträglich additiv erweiterbar.

## 12. Risiken / Annahmen / überholte Aussagen

1. MCP Events ist Design-Sketch-Niveau (experimental, kein Major-Spec-Release). Der Web-Handler ist bewusst klein: nur list/subscribe/unsubscribe plus Webhook-Send. Semantikänderungen der Extension werden additiv adaptiert; Pull-Pfad und Auth-Regeln bleiben unabhängig.
2. Die reale Workspace-Verfügbarkeit (Bitsurance ChatGPT-Work) bleibt praktische externe Abnahme. Der Schritt ist Teil des bestehenden Connected-/B2-Laufs, kein separates Abnahme- oder Wakeup-Testverfahren; bis zu echter Workspace-Evidence ist der Status `PENDING_EXTERNAL`.
3. Die frühere Note "automatisches ChatGPT-Wakeup in offiziellen Quellen nicht belegt" ist überholt. Die offizielle OpenAI-Doku (abgerufen 2026-10-03) dokumentiert event-triggered Work-Flows mit MCP Events. Es bleibt ausdrücklich die Regel: kein End-to-End-Wakeup-Claim ohne echte Workspace-Evidence.
4. Falls ein Deployment-Kontext aiohttp explizit vorhanden hält, bleibt der Code stdlib-only; aiohttp wird nicht mehr benutzt und die Importstellen werden endgültig entfernt.
5. Attention Queue bleibt außerhalb des Scopes (Board-Korrektur vom 2026-10-02); kein zusätzliches Board-Ticket.
6. Es gibt keine automatische Approval- oder automatische Folgeausführung; jedes Command bleibt vollständig an die menschliche Approval-Semantik gebunden.


## 13. Implementierungsnachweis (2026-10-03)

Umgesetzter Code und Tests anhand dieses Plans:

- `admin_in_the_loop.py`: der Embedded-Bridge-Block arbeitet vollständig auf der Standardbibliothek. `BridgeService` bedient POST `/mcp`, `/v1/*` (Controller) mit 1:1-Guards (Origin/Host/Dupletten-Header/Limits/Rate/Inflight), 202-Notifications, threading-Long-Poll; `BridgeStore` erhält die `subscriptions`-Tabelle und die Outputs-Spalten `delivered`/`delivered_at`; `events/list|subscribe|unsubscribe` werden nur in der modernen Variante advertiseiert; Standard-Webhooks-Signierung mit stdlib hmac; Callback-Verifikation, SSRF-Prüfung (nur HTTPS, nur öffentliche Ziele, fixed destination, keine Redirects); Retry mit 3 Versuchen; 2xx-Ack markiert `delivered`. `AppCore` speichert transferred Submissions; `approve()` nimmt eine explizite `submission_id`; die Review-UI zeigt den Bindingsslot.
- `tests/test_connected_mvp.py`: zusätzliche Klassen `EventSubscriptionTests` und `EventDeliveryTests` mit In-Process-`ServiceHarness`; Abdeckung von Discovery, Subscribe/unsubscribe/Refresh/Idempotenz, Validation-Fehlern (-32602/-32011/-32013/-32015), Challenge/Mismatch, Standard-Webhooks-Signatur, SSRF-Adress-Guards, Event-Nur-nach-Approval, Payload-Shape, Filter-Korrelation, Pull-Fallback bei nicht bestätigtem Zustellversuch, Delivery-Idempotenz mit gleichem eventId/Submissions-Retry, Restart-Persistenz und multi-flight genauer Binding im `ConnectedRoundTripTests`.
- `skill/SKILL.md`: Event-Continuation-Abschnitt (Plan §6.3). README/IMPLEMENTATION_STATUS/Local_Integration_README ohne aiohttp; historische Konzept-Dokumente bleiben unverändert.

Historischer berichteter Testnachweis der lokalen Regression (2026-10-03; aktueller Rerun in §15):

1. `python3 admin_in_the_loop.py --self-test` → 49/49 OK.
2. `python3 tests/test_connected_mvp.py` → 55 Tests, OK (2 skips, offizielles Tunnel-Binary nicht im Environment).
3. `xvfb-run -a python3 tests/gui_smoke_test.py` → früher berichtet 16/16 OK unter Xvfb; kein Rerun-Nachweis dieses Korrektur-Passes.
4. `import admin_in_the_loop` ohne aiohttp-Installation; keine aiohttp-Referenz mehr im Bridge-/Test-/Skill-Kontext.

Damals offen: Der praktische E2E-Nachweis im echten ChatGPT-Work-Workspace (Event-Continuation ohne zusätzlichen User-Turn) war noch nicht in den Connected-Testpfad integriert. Der aktuelle Stand ist in §16 dokumentiert. Kein Release-/Deployment-Schritt wurde durchgeführt.


## 14. Repair-Pass (2026-10-03, Review-H-/M-Befunde)

Historischer Stand des ersten Repair-Passes. Seine Vollständigkeitsbehauptung wurde durch den nachfolgenden unabhängigen Final-Review **REVIEW_FAIL** (`msg_103302d02001Jxst4Z31CTiJJb`, F1–F6) widerlegt. Insbesondere H3/M1 waren noch nicht vollständig behoben; die späteren Korrekturen sind in §15 und der aktuelle QC-Stand in §16 beschrieben.

1. **H1 webhook-ID (FIXED):** `webhook_call` signiert und sendet `webhook-id` als eventId für Event-Deliveries bzw. `msg_*` für Verifikations-/Control-Envelopes, nie die Subscription-ID; `X-MCP-Subscription-Id` bleibt der Routing-Header. Test: realer Empfänger verifiziert beides.
2. **H2 SSRF (FIXED):** die aufgelöste Adressliste darf keine private/lokale Adresse enthalten; gemischtes DNS (public+private) wird verworfen; der post-connect destination check bleibt. Tests: mixed private und all-private werden ohne jeden Verbindungsaufbau abgewiesen.
3. **H3 Teilrepair (historisch, Abschlussclaim superseded):** queued Deliveries revalidierten unsubscribe/expiry/revocation vor dem Transport. AI-Key-Auth allein belegte aber keine menschliche Chatbindung (F1), und der Recheck vor einem late ACK fehlte (F5). Erst §15 schließt diese Lücken mit neuen Regressionen.
4. **H4 FIXED / M1 Teilrepair (historisch):** `approve(..., explicit_binding=True)` behandelte eine explizit unbound Wahl korrekt. Die deaktivierte UI-Auswahl war noch nicht gegen Candidate-Änderungen eingefroren, und unbekannte Labels wurden still unbound (F6). Core-Tests belegten die explizite Payload-Bindung, nicht den vollständigen GUI-Freeze; §15 ergänzt den Nachweis.
5. **H5 unbound Readback (FIXED):** der generelle Pull überspringt nur bound+delivered Outputs; unbound Outputs bleiben nach bestätigtem Push über den generellen Pull lesbar (nichts geht verloren); bound Outputs bleiben via `get_approved_output(submission_id)` bis zur Retention lesbar.
6. **H6 Subscribe-Contract (FIXED):** `cursor: null` wird wie dokumentiert akzeptiert (Replay bleibt unsupported); ttlMs wird in Millisekunden interpretiert, `refreshBefore = now + ttlMs/1000` mit Min/Max-Clamp. Tests decken beide Punkte.
7. **H7 TLS-Stall (FIXED):** der TLS-Handshake läuft im Per-Request-Thread, nie im Accept-Thread; ein hängender Client blockiert den Listener nicht mehr. Test: quälender Client, gleichzeitig echter TLS-Request.
8. **M2 Framing (FIXED):** doppelte Content-Length (400) und Transfer-Encoding (411) werden strikt abgelehnt; Guards umfassen beide Header.
9. **M3 Store-Serialisierung (FIXED):** claim/confirm/submit/issue/approved_output/cursor laufen in einem Store-Lock; submit fängt den Unique-Conflict atomar ab (keine Doppel-Rows, keine doppelte Verarbeitung). Test: 6 Threads, ein Proposal.
10. **M4 echte Transport-Tests (FIXED):** WebhookTransportTests bauen einen lokalen HTTPS-Receiver mit echter Standard-Webhooks-Signaturprüfung; damit laufen Challenge-Verifikation, Event-Delivery, ID-Assertions (webhook-id == eventId) (webhook-id == eventId, X-MCP-Subscription-Id), Delivery nach Ack, 5xx-Verweigerung und der echte webhook_call Durchlauf; zusätzlich SSRF-, TTL-, unbound-readback-, framing-, Thread- und Restart-Persistenzfälle.
11. **M5 Skill-Wortlaut (FIXED):** nach bestätigtem Push: bound Outputs lassen get_submission_status=false, aber bleiben über get_approved_output(submission_id) bis zur Retention lesbar; unbound Outputs über den general pull; verlorene/Nicht-Zustellungen werden via Pull nachgeholt; Continuation-Claim ohne gelesenen kanonischen Output bleibt verboten.

Repair-Testnachweis (2026-10-03):

1. `python3 admin_in_the_loop.py --self-test` → 49/49 OK.
2. `python3 tests/test_connected_mvp.py` → 107 Tests OK (2 skips: offizielles Tunnel-Binary im Environment nicht geliefert).
3. `xvfb-run -a python3 tests/gui_smoke_test.py` → früher berichtet 16/16 OK unter Xvfb; im unabhängigen Final-Review und diesem F1–F6-Pass mangels lokaler Display-/Xvfb-/xterm-Umgebung nicht erneut ausführbar.

Offen (unverändert):

- **Produkt-Commit-Baseline:** HEAD enthält nur LICENSE plus Task-Dokumente; die gesamte Produktbasis ist historisch untracked. Ohne Operator-Entscheidung zum historischen Import wird kein produktseitiger Commit erstellt; HEAD bleibt d04a596, Produktcode und Korrekturen bleiben im Arbeitsverzeichnis. Der neue dateibasierte Review-Snapshot (§15) ersetzt keine historische Git-Importentscheidung.
- **Externe Abnahme (damaliger Stand):** der ChatGPT-Work-Workspace E2E-Nachweis (Event-Continuation ohne zusätzlichen User-Turn) stand noch aus; inzwischen ist er als Schritt im regulären Connected-Lauf definiert (siehe §16).
- MCP-Events bleibt Design-Sketch-verankert; nachträgliche Standard-Änderungen werden additiv adaptiert, ohne Pull oder Auth zu verändern.


## 15. Begrenzter F1–F6-Korrektur-Pass (2026-10-03, historischer Stand)

Der Final-Review des unveränderten ersten Repair-Snapshots ergab **REVIEW_FAIL**, terminales Ergebnis `msg_103302d02001Jxst4Z31CTiJJb`. Der damalige Pass reparierte ausschließlich die sechs bestätigten Findings und unmittelbar nötige Regressionen/Dokumentationskorrekturen. Der im Anschluss durchgeführte unabhängige Review ist inzwischen **REVIEW_PASS**; §16 ist der aktuelle maßgebliche QC-/Connected-Teststand.

### 15.1 Preflight / Git / Ownership

- C/P zuerst gelesen. `pwd -P` und Repo-Root: `/Users/admin/Documents/github/admin-in-the-loop`; Git-Dir/Common-Dir `.git`.
- Branch/HEAD: `main @ d04a59699222827d7bfac85cf47b39f79eb6797c`; genau ein Worktree unter diesem Pfad.
- Vollständiger Status inkl. untracked vor dem ersten Write erfasst: drei getrackte Dateien (LICENSE, C, P), C/P bereits task-owned modifiziert, 37 nonignored untracked Dateien, keine Indexänderungen. Dazu zählen Produktbasis, andere Task-Dokumente, Branding, historische Berichte und vorhandene Cache-/Desktop-Metadaten.
- Alle 40 Dateien entsprechen exakt dem Review-Subjekt. Manifest-SHA-256: `2b7b731c2141727aaf4200e3728b557051dcba614dbcc27ead04855aeebfdfd2`.
- Bekannte Review-/Analyse-Sessions idle; Session-Status vor Übernahme und nach Tests zeigte nur diese Fix-Session busy. Prozessprüfung zeigte keinen weiteren Produkt-Writer; keine konkurrierende Änderung oder unerklärter Drift. Arbeitsbaum für diesen Pass seriell übernommen.
- Keine fremden Inhalte gelöscht/überschrieben oder historische Baselines importiert/gestaged. Kein Commit, Push, Tag, Release, Deployment oder Veröffentlichung.

### 15.2 Fixes und konkrete Regressionsevidenz

Alle folgenden Tests stehen in `tests/test_connected_mvp.py`:

1. **F1 HIGH — human chat-binding geschlossen.** `BridgeService.event_arguments` prüft den menschlich übergebenen `arguments.conversation`-Code mit derselben Form-/constant-time-Regel wie die Tool-Grenze. Subscribe, Refresh/Secret-Update und passendes Unsubscribe verwenden diese Prüfung; `events/list.inputSchema` advertised das Pflichtfeld. Der Code wird vor Filterkanonisierung entfernt, weder persistiert noch emittiert. `subscriptions.bound` migriert alte ungeprüfte Zeilen fail-closed auf 0; nur autorisierter Refresh/Subscribe aktiviert sie. Der Principal wird nach Callback-Verifikation unter Store-Lock erneut geprüft.
   - `EventSubscriptionTests.test_missing_wrong_binding_cannot_subscribe_receive_or_ack`: fehlender/falscher/None-Code → keine Subscription/Callback-Verifikation/Metadata-Delivery, `delivered=0`, Status und generischer Pull verfügbar; gültiges Binding funktioniert.
   - `test_refresh_secret_update_and_unsubscribe_require_binding`: falsches/fehlendes Binding verändert weder Secret noch TTL noch Subscription; korrektes Unsubscribe funktioniert.
   - `CorrectionRegressionTests.test_legacy_unbound_subscription_needs_authorized_refresh`: tatsächliche Schema-Upgrade-Fixture, alte Zeile liefert nicht; gültiger Refresh aktiviert dieselbe ID ohne Code im Filter.
   - `test_revocation_during_callback_verification_rejects_subscription`; positive Event-Delivery prüft Payload-Felder und das Fehlen des Codes.

2. **F2 HIGH — Callback-Authority geschlossen.** `webhook_call` setzt HTTP `Host` explizit aus dem Callback-Hostname, mit IDNA-/Port-/IPv6-Klammerformat. `WebhookConnection` verbindet weiterhin zur validierten numerischen Destination; TLS-SNI/Zertifikatsprüfung verwendet weiterhin den ursprünglichen Hostnamen, Peer-Adresscheck und SSRF-Guard bleiben aktiv.
   - `WebhookTransportTests.test_delivery_signature_identifiers_and_delivery_state`: echter lokaler HTTPS-Empfänger routet ausschließlich auf `callback.example:<port>`, verifiziert Standard-Webhooks-Signatur, Challenge und Event. Numerischer Host ergäbe 421.
   - Die TLS-Fixture vertraut nur ihrem Zertifikat ausdrücklich; `check_hostname`/Zertifikatsprüfung bleiben aktiv. Nur DNS/Loopback-Admissibilität sind für die lokale Fixture ersetzt.
   - `test_callback_certificate_hostname_mismatch_is_rejected`: falscher TLS-Name → `tls_error`, kein HTTP-Request am Empfänger.
   - `test_host_authority_default_explicit_port_and_ipv6`: Default-Port, explizites 443, IPv6 mit/ohne Port.

3. **F3 HIGH — Pre-dispatch-Limit und Gesamtfrist geschlossen.** Beide `BridgeHTTPServer`-Listener teilen 64 Connection-Slots. Admission liegt vor `ThreadingMixIn.process_request`, also vor Thread-Erzeugung, TLS und Parsing. Überzählige Connections schließen fail-closed; Exception- und Worker-`finally` geben Slots zurück. `BoundedReader` erzwingt eine monotone 5-s-Gesamtfrist für TLS/Request-Line/Headers/Body; `read1` gibt Kontrolle nach höchstens einem Socket-Receive zurück und setzt jeweils die Restfrist. Long-Poll nach fertig gelesener Anfrage bleibt unverändert.
   - `test_incomplete_request_lines_bound_threads_and_release_connections`: 64 offene unvollständige Request-Lines, acht weitere MCP-Connections plus Controller-Connection abgewiesen, höchstens 64 Worker, `inflight=0`; nach Close wieder erfolgreicher Request.
   - `test_trickle_body_exceeds_monotonic_total_deadline`: Fortschritt alle 0,9 s, trotzdem nach Gesamtfrist HTTP 408/body_timeout; kein Proposal angelegt, Inflight/Serving wieder frei.
   - `test_trickle_headers_exceed_total_deadline_and_release_slot`: kontinuierliche Header-Trickle wird trotz Aktivität geschlossen; anschließender Request erfolgreich. Bestehender TLS-Stall-/Long-Poll-/Framing-Test grün.

4. **F4 MEDIUM — atomare Quota geschlossen.** `store_subscription` prüft nach Callback-Verifikation active Quota unter demselben Store-Lock wie Insert/Refresh. Abgelaufene bestehende Zeilen zählen als neue Allocation, aktive Refreshes bleiben am Limit möglich.
   - `test_parallel_subscribes_recheck_quota_after_verification`: Start bei 63 aktiven Zeilen; Barrier zwingt beide Callback-Verifikationen in denselben Konkurrenzfall (ein neuer Subscribe, ein expired Refresh). Exakt ein Erfolg und ein -32013, niemals mehr als 64 aktive Zeilen.
   - `test_expired_refresh_at_full_quota_is_rejected` und die bisherigen Cap-Tests prüfen jetzt tatsächlich Limit 64 plus aktiven Refresh.
   - Während dieser Regression reproduzierte unmittelbar nötige Query/Fetch-Korrektur: Shared-SQLite-Cursor wurden in `authenticate`, `count_subscriptions`, `find_subscription` außerhalb des Execute-Locks konsumiert. Das erzeugte sporadisch 401/500 bzw. fehlendes Count-Ergebnis. Diese drei Query+Fetch-Sequenzen sind jetzt vollständig gelockt; keine Auth-Regel gelockert. Ein Zwischenlauf hatte 1/17 Fehler, die Diagnose 3/20 Race-Fehler; nach der Korrektur 20/20 Wiederholungen und final 17/17 sowie volle Suite grün. Assertions/Quota/Timeouts wurden nicht abgeschwächt.

5. **F5 MEDIUM — late ACK geschlossen.** `mark_delivered(output_id, subscription_id)` revalidiert aktive human-bound Subscription und gepairten AI-Principal unter Store-Lock direkt vor dem Update; das SQL-Update enthält denselben Authorization-EXISTS-Prädikat gegen konkurrierenden Widerruf über eine andere SQLite-Verbindung. Doppelte gültige ACKs bleiben idempotent. Ein schon gestarteter Send ist nicht rückrufbar, aber eine unautorisierte späte Antwort konsumiert keinen Output.
   - `WebhookTransportTests.test_late_transport_ack_after_revocation_keeps_pull_fallback`: echte HTTP-Delivery pausiert nach Requestempfang vor 200; Principal wird widerrufen; nach 200 bleiben `delivered=0`, `delivered_at=None`, Status verfügbar und ein erneut autorisierter Principal liest den generischen Pull.
   - `test_ack_update_authorization_is_atomic_with_external_sqlite_revocation`: zweite SQLite-Verbindung widerruft zwischen Vorprüfung und Update; SQL-Autorisierung verhindert Markierung.
   - `test_late_ack_after_unsubscribe_or_expiry_cannot_suppress_pull` ergänzt die bereits bestehenden queued/pre-flight-Rechecks.

6. **F6 MEDIUM — Review-Binding-Anzeige geschlossen.** `ReviewUI` verwendet im Review eine selection-only (`readonly`) Combobox und rejected unbekannte Labels explizit vor `core.approve`. Für dieselbe Review-ID werden nach Approve Label, Mapping und Sichtbarkeit nicht mehr aus veränderlichen Candidates rekonstruiert, über SENDING/DELIVERY_UNCERTAIN/ACKNOWLEDGED hinweg. Ein neuer Review erhält eine neue Auswahl; explizit unbound bleibt eine gültige menschliche Wahl.
   - `BindingDisplayRegressionTests.test_selection_only_and_unknown_label_is_rejected`: arbitrary Label → Fehler, kein Approve-Aufruf; bekannte unbound-Auswahl → explizit None.
   - `test_approved_binding_frozen_across_candidate_expiry_removal_and_disconnect`: tatsächliche `ReviewUI`-Methoden, zuerst genau gewähltes Binding an Approve, dann Candidate-Expiry/Removal/Disconnect und Delivery-State-Wechsel ohne Label-/Mapping-/Sichtbarkeitsänderung; neuer Review ohne geerbte Auswahl.
   - Das sind Headless-Methodentests, keine Tk-/Desktop-Abnahme. Core-Payload-Binding/Unbound/identische Retry-Snapshots weiterhin durch bestehende ConnectedRoundTripTests belegt.

Damit sind die durch F1/F5/F6 widerlegten H3-/M1-Abschlussclaims erst jetzt lokal mit neuen Regressionen geschlossen. Unabhängiger Review und externe Acceptance bleiben ausstehend.

### 15.3 Finaler Test-/Qualitätsnachweis

Alle Tests mit `PYTHONDONTWRITEBYTECODE=1`; temporäre Testzustände außerhalb des Repos, bestehende Cache-Dateien unverändert.

| Gate | Finaler Nachweis |
|---|---|
| Gezielte F1–F6-Auswahl | 17 Ausführungen, 17 PASS, 0 Skips |
| Zusätzliche F4-Race-Wiederholung nach Query/Fetch-Fix | 20 Ausführungen, 20 PASS, 0 Skips |
| `python3 admin_in_the_loop.py --self-test` | 49 Ausführungen, 49 PASS, 0 Skips |
| `python3 tests/test_connected_mvp.py` | 133 Ausführungen, 131 PASS, 2 Skips; damals 86 unterschiedliche Methodenimplementierungen, übrige Wiederholungen durch Vererbung |
| `python3 tests/test_local_setup.py` | 14 Ausführungen, 13 PASS, 1 Skip |
| GUI-Smoke | Nicht ausführbar: DISPLAY nicht gesetzt, Xvfb/xvfb-run/xterm fehlen; Tkimport allein ist keine Display-Evidenz. Kein simuliertes GUI-PASS. |

Skips ausschließlich wegen nicht bereitgestelltem offiziellen Tunnelbinary (zwei Connected-, ein Setup-Fall). Die früher berichteten 16 GUI-Tests wurden nicht rerun. Pull mit/ohne Submission, unbound Readback, Callback/Events/TTL/Restart, Approvals/negative Chatbindung, SSRF/private/mixed DNS, TLS/Pairing, HTTP-Framing, Submission-/Output-Idempotenz und Long-Poll bestehen weiterhin. Keine Security-check-Bypasses oder Test-Abschwächungen.

Gezielte Auswahl reproduzierbar über `PYTHONPATH=tests python3 -m unittest -v` mit `test_connected_mvp.CorrectionRegressionTests`, `test_connected_mvp.BindingDisplayRegressionTests`, den beiden neuen `EventSubscriptionTests`-Binding-Fällen sowie vier `WebhookTransportTests`-Fällen (Delivery/Host, falscher TLS-Name, Authority-Format und late ACK). Diese Auswahl umfasst 17 Tests; Bytecode wie oben deaktivieren.

### 15.4 Exakte Inventur / reproduzierbarer Kandidat

Gegen die unverändert gesicherte 40-Dateien-Review-Basis genau diese fünf Dateien geändert, keine hinzugefügt/gelöscht:

1. `admin_in_the_loop.py` — F1–F6 und unmittelbar nötige Auth-/Quota-Cursor-Serialisierung.
2. `tests/test_connected_mvp.py` — gezielte Regressionen, gültige Binding-Argumente für positive bestehende Tests, echte TLS-/Host-Prüfung der lokalen Fixture und tatsächliche Cap-Assertions.
3. `skill/SKILL.md` — Pflichtbinding bei Event-Operationen/Legacy-Refresh und ACK-Autorisierung präzisiert.
4. `.opencode/tasks/task-task_98110f66-9726-5a10-8c02-91cc5e0cedfc.compact.md` — Review-FAIL, Korrekturstand, Tests, Inventur, nächster Review.
5. Dieses P — maßgebliche Anforderungen und historische H3-/M1-Claims korrigiert, §15 ergänzt.

Übrige 35 Review-Dateien byte-identisch, einschließlich fremder Task-Dokumente, historischer Berichte, Branding/README-Dateien und vorhandener Bytecode-/Desktop-Metadaten. Git-Statusstruktur bleibt C/P modifiziert plus dieselben 37 untracked Dateien; Index/HEAD/Branch unverändert.

Snapshot-Hilfsartefakte ausschließlich außerhalb des Arbeitsbaums unter `/tmp/opencode/`: `aitl-f1-f6-before.tar` (vor jedem Produkt-Write gesichert), `aitl_f1_f6_snapshot.py`, `aitl-f1-f6-before.manifest.sha256`, `aitl-f1-f6-candidate.manifest.sha256`, `aitl-f1-f6-candidate.tar`. Das vorhandene unabhängige Review-Probeskript blieb unverändert. Diese VM-lokalen Artefakte sind keine Produktdateien/Commits.

Kandidatenmenge identisch zum Review: Ausgabe von `git ls-files --cached --others --exclude-standard -z` in **Git-Reihenfolge**, keine Sortierung, genau 40 Dateien. Manifest pro Datei: SHA-256 des Inhalts, zwei ASCII-Leerzeichen, relativer Pfad, LF; SHA-256 über die gesamten Manifestbytes. Nicht SHA-256 einer alphabetisch neu sortierten Liste. Das reproduzierbare USTAR-Archiv nutzt dieselbe Reihenfolge/Bytes, mtime/uid/gid=0 und bestehende Dateimodi.

Inventur/Archiv reproduzieren: `python3 -B /tmp/opencode/aitl_f1_f6_snapshot.py --snapshot`; geänderte Dateien/Diff zusätzlich mit `--diff`. Manifest und Archive-Hash werden nach dem letzten C/P-Write im terminalen Abschlussbericht geliefert (keine selbstreferenzielle Einbettung des Gesamtmanifest-Hashes in C/P).

Manifest-Hash ohne VM-Hilfsskript aus dem Projekt-Root reproduzieren:

```bash
python3 -B -c 'import subprocess,hashlib; paths=[p for p in subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"]).split(b"\0") if p]; manifest=b"".join(hashlib.sha256(open(p,"rb").read()).hexdigest().encode()+b"  "+p+b"\n" for p in paths); print(len(paths), hashlib.sha256(manifest).hexdigest())'
```

### 15.5 Damalige Abschlussgrenzen

- Zu diesem Zeitpunkt wurde ein neuer unabhängiger Review empfohlen; er wurde anschließend durchgeführt und bestand (siehe §16).
- Historische Git-Importentscheidung bleibt offen; dieser Pass durfte ohne Commit im Arbeitsbaum abgeschlossen werden. Keine Produkt-/Baseline-Datei gestaged/committed, kein Push/Tag/Release/Deployment/Publishing.
- Der externe ChatGPT-Work-E2E/Host-Wakeup/Continuation-Nachweis stand zu diesem Zeitpunkt aus; inzwischen ist er als offener Schritt in den regulären Connected-Lauf integriert (siehe §16). Es wird weiterhin kein PASS aus lokalen Fixtures abgeleitet.
- Signing-Key-Rotation/Host-Interoperabilität und native Desktop-Abnahme bleiben externe/unverifizierte Grenzen des Reviews, keine zusätzlich implementierten Features in diesem F1–F6-Scope.


## 16. REVIEW_PASS und regulärer B2-/Connected-Continuation-Schritt (2026-10-03)

### 16.1 Maßgebliche QC-Evidenz und Entscheidung

- Neueste unabhängige Prüfung: **REVIEW_PASS**; Findings F1–F6 geschlossen. Freigegebene Produktbasis `main @ d04a59699222827d7bfac85cf47b39f79eb6797c`.
- Exakter Review-Kandidat: 40 Dateien, Manifest-SHA-256 `ab50c4a490650cee8e484aed1bf51fd2438ed538644521a166e5d2c2f7ed1caa`; deterministischer USTAR-SHA-256 `7073ac42f5e2a9a7c65605d2728313a52e27ee795b1e00f2be29febbcfea3b19`. Review-Freigabe: `Proceed to external ChatGPT-Work E2E: Yes`.
- Operatorentscheidung 2026-10-03: **kein separater ChatGPT-Work-Wakeup-Sonderlauf**. Der reale Host-/Event-Continuation-Nachweis ist ein regulärer externer Schritt im vorhandenen klassischen B2/E2E/Connected-Akzeptanzlauf.
- Worktree-Preflight vor diesem Pass bestätigte denselben 40-Dateien-Manifesthash; `main @ d04a59699222827d7bfac85cf47b39f79eb6797c`, ein Worktree, Index leer, C/P bereits task-owned dirty, 37 nonignored untracked Dateien. Ein konkurrierender Code-Writer war nicht sichtbar; der laufende Taskboard-Dienst ist Session-Infrastruktur.

### 16.2 Tatsächlich vorhandener kanonischer Pfad

- Repository-Suche ergab keinen wörtlich `B2` benannten Runner und keine separate B2-Suite. Der vorhandene kanonische Connected-/E2E-Pfad ist `python3 tests/test_connected_mvp.py`: die Datei nutzt Python-`unittest` und hat einen ausführbaren `unittest.main`-Einstieg. Die historische Testanleitung `tests/Local_Integration_README.md` führt denselben Runner.
- Der klassische Controller-Roundtrip liegt in `ConnectedRoundTripTests` und benutzt `IntegrationFixture`: reale lokale HTTP/MCP-Sockets, den Controller-BridgeWorker und PTY-Helper/Subprozess. `EventDeliveryTests` und `WebhookTransportTests` befinden sich in derselben normalen Suite. Es wurde keine parallele Suite, kein Sonder-Runner und kein Wakeup-Teststrang erzeugt.

### 16.3 Integrierter lokaler Continuation-Fall

`ConnectedRoundTripTests.test_approved_output_event_continuation_uses_exact_canonical_read` läuft regulär mit dem Connected-Testpfad und deckt ab:

1. `submit_proposal` stellt einen Command-Vorschlag bereit; vor Review/Freigabe gibt es weder PTY-Ausführung noch Output/Event.
2. Menschliche Command-Freigabe über `AppCore` führt genau den überprüften Command im PTY aus.
3. Der Output wird separat im Review ausgewählt, bearbeitet und mit der exakten `submission_id` menschlich freigegeben.
4. Erst danach wird `approved_output.available` erzeugt; das Event enthält die Submission-/Output-Korrelation, aber weder Conversation-Code noch Output-Text.
5. Der lokale Host-Schnittstellen-Fall liest den kanonischen Output mit exakt passender `conversation`-Bindung und `submission_id`; ein falsches Binding scheitert. Der gelesene Output stimmt mit Event-ID, Submission und freigegebenem Text überein.
6. Idempotenter Upload-Replay erzeugt kein zweites Event; ein wiederholtes Event mit derselben Event-ID wird dedupliziert, `delivered`/`delivered_at` bleiben idempotent, und weder zweiter Command noch ungefragter Folge-Proposal entsteht.

Für diesen deterministischen Connected-Test verwendet `ServiceHarness` lokale echte HTTP/MCP-Server und einen kontrollierten Webhook-Transportstub. Pull-Fallback bei fehlgeschlagener Zustellung und tatsächliche lokale TLS-/Signatur-Webhook-Übertragung werden durch weitere Tests im selben Runner abgedeckt, unter anderem `EventDeliveryTests.test_failed_delivery_keeps_output_available` und `WebhookTransportTests.test_delivery_signature_identifiers_and_delivery_state`. Kein lokaler Test simuliert das ChatGPT-Work-UI als bestanden.

### 16.4 Externer Host-Schritt im selben Lauf

`ConnectedRoundTripTests.test_chatgpt_work_event_continuation_without_another_user_turn` steht im selben `unittest`-Lauf und wird hier mit dem Skip-Grund `PENDING_EXTERNAL: real ChatGPT-Work host/workspace is unavailable here` ausgewiesen. Das ist ein expliziter offener Akzeptanzpunkt, kein PASS.

Externe Abnahmesequenz: im echten konfigurierten ChatGPT-Work-Workspace einen Proposal-/Command-Review auslösen, Command menschlich freigeben und dessen Ausführung beobachten, den Output separat menschlich freigeben, das MCP-Event im selben Chat ohne zusätzlichen User-Turn empfangen, per exakt korrelierter Submission und Conversation den kanonischen `get_approved_output`-Readback verarbeiten und bestätigen, dass kein Approval umgangen und kein Folge-Command ohne neuen Vorschlag/Approval ausgeführt wird. Workspace-/Conversation-Evidence gehört zum regulären B2-/Connected-Report; Secrets und Chatbindungscode werden nicht protokolliert.

Status dieser Worker-Ausführung: **PENDING_EXTERNAL / UNVERIFIED**. Die reale Host-Oberfläche/Workspace ist von hier nicht zugänglich. Der Board-Task bleibt daher `in_progress`; kein `done`-Move bis der externe Nachweis vorliegt.

### 16.5 Lokale Verifikation und genaue Resultate

Alle Tests liefen mit `PYTHONDONTWRITEBYTECODE=1`.

| Check | Resultat |
|---|---|
| Fokussiert: neue Connected-Continuation, Event nur nach menschlicher Output-Freigabe, fehlgeschlagene Delivery mit Pull-Fallback und lokaler echter HTTPS-Webhook | **4/4 PASS**, 0 Skips (6.590 s) |
| `PYTHONDONTWRITEBYTECODE=1 python3 admin_in_the_loop.py --self-test` | **49/49 PASS**, 0 Skips (8.070 s) |
| `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_connected_mvp.py` | **135 Ausführungen: 132 PASS, 3 Skips** (113.710 s); 2 Skips wegen nicht bereitgestelltem offiziellem Tunnelbinary, 1 `PENDING_EXTERNAL` ChatGPT-Work-Schritt |
| `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_local_setup.py` | **14 Ausführungen: 13 PASS, 1 Skip** (5.531 s; offizielles Tunnelbinary fehlt) |
| GUI-Smoke | Nicht ausführbar: `DISPLAY` fehlt, `Xvfb`/`xvfb-run`/`xterm` fehlen; kein GUI-PASS behauptet |

Fokussierter Lauf:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests python3 -m unittest -v \
  test_connected_mvp.ConnectedRoundTripTests.test_approved_output_event_continuation_uses_exact_canonical_read \
  test_connected_mvp.EventDeliveryTests.test_event_only_after_human_approved_output \
  test_connected_mvp.EventDeliveryTests.test_failed_delivery_keeps_output_available \
  test_connected_mvp.WebhookTransportTests.test_delivery_signature_identifiers_and_delivery_state
```

Der vormals in C/P angegebene Wert 85 unterschiedlicher Methodenimplementierungen war ein Zählfehler; für den vorhandenen Stand sind 86 unabhängig bestätigt. Zwei neue Testmethoden bringen die aktuelle statische Zahl auf **88**; die Suite führt wegen geerbter Tests 135 Testfälle aus. Produktcode blieb unverändert; geändert wurden Test-Harness und task-owned C/P. Kein Staging, Commit, Push, Tag, Release, Publish oder Deployment.

### 16.6 Abschluss / Next Step

- Der geprüfte REVIEW_PASS bleibt die maßgebliche QC-Evidenz; die test-/dokumentationsseitige Integration hier ist keine neue Produktänderung.
- **NEXT_STEP:** den in `ConnectedRoundTripTests.test_chatgpt_work_event_continuation_without_another_user_turn` beschriebenen realen Host-Schritt als regulären Schritt desselben B2-/Connected-Akzeptanzlaufs ausführen, `PENDING_EXTERNAL` im Report durch echte Evidence ablösen und erst danach die Board-Abschlussentscheidung treffen.

### 16.7 Lokaler Git-Checkpoint

- Re-Preflight nach erneuter C/P-Lektüre: Repo-Root `/Users/admin/Documents/github/admin-in-the-loop`, Branch `main`, Vorab-HEAD `d04a59699222827d7bfac85cf47b39f79eb6797c`, genau ein Worktree, Index leer. Kein konkurrierender Git-/Test-Writer.
- Der gespeicherte Review-Snapshot wurde byteverifiziert: 40 Dateien, Manifest `ab50c4a490650cee8e484aed1bf51fd2438ed538644521a166e5d2c2f7ed1caa`, USTAR `7073ac42f5e2a9a7c65605d2728313a52e27ee795b1e00f2be29febbcfea3b19`. Die Baseline-Dateien für den Import stimmen bytegenau mit dem Snapshot überein; `.DS_Store`, alle `__pycache__`/`*.pyc`, fremde Task-Dokumente und C/P wurden nicht in den Baseline-Commit genommen. `.gitignore` wurde geprüft: einziges Muster `.opencode-vm/`, eng auf VM-Runtime-Dateien begrenzt.
- **Baseline-Import:** Commit `05dea5aa5bf19f2901585d31aa6229679c382c82`, Parent `d04a59699222827d7bfac85cf47b39f79eb6797c`, Nachricht `task_98110f66: import reviewed project baseline`. 25 explizit gestagte geprüfte Projektpfade: `.gitignore`, `IMPLEMENTATION_STATUS.md`, `README.md`, `admin_in_the_loop.py`, `doc/Admin-in-the-Loop_Konzept_V0.7.md`, `doc/RELEASE_V0.0.1.md`, `doc/TASKBOARD_PROPOSAL.md`, `doc/assets/branding/README.md`, die sieben übrigen Branding-Dateien unter `doc/assets/branding/`, `skill/HANDOFF_SKILL_NOTE.md`, `skill/SKILL.md`, fünf Testdokumente unter `tests/`, `tests/gui_smoke_test.py`, `tests/test_local_setup.py` und der Snapshot-Stand von `tests/test_connected_mvp.py`. Der gestagte Inhalt aller 25 Pfade stimmte bytegenau mit dem Review-Snapshot überein.
- **B2-Continuation:** Commit `00066abc15269ffea9f0411ed47774e2ef20373e`, Parent `05dea5aa5bf19f2901585d31aa6229679c382c82`, Nachricht `task_98110f66: cover approved-output continuation`. Einziger Pfad: `tests/test_connected_mvp.py`; der tatsächliche Git-Diff enthält **139 Einfügungen** für die genau zwei Continuation-Testmethoden. Die frühere C/P-Angabe `+135` war eine Zeilenzählung des Unified-Diff-Hunks, nicht der Git-Insertionszähler. Current test SHA-256 `51a8d414abdc135899e512697a8738180eb8fd5a166e9e953234db6f945dcc30`; zuvor ausgeführter Connected-Lauf war auf demselben Inhalt.
- Produktcode wurde nicht geändert: `admin_in_the_loop.py` SHA-256 ist vor/nach Import `8fb7f15dc505b09b2e71a51d6f899e13388b0ca0f1f1d1ed631c6aaef173497e`. Keine Tests wurden wegen des Imports wiederholt. C/P und vorbestehende fremde Task-Dokumente wurden vom Baseline- und B2-Codecommit getrennt gehalten; C/P werden separat als task-owned Dokumentation festgehalten.
- Der reale ChatGPT-Work-Hostschritt bleibt **PENDING_EXTERNAL / UNVERIFIED**; Board-Task `in_progress`, kein Board-Done. Nachfolgender Dokumentationscommit enthält nur die beiden kanonischen Task-Dokumente.
