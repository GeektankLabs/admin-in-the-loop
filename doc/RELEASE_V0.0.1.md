# v0.0.1 – Release-Vorbereitung

**Status: historischer Dokumentationsentwurf, nicht veröffentlicht.**
Stand: 1. Oktober 2026. Zugeordnet zum Board-Task „Initial Git publish & v0.0.1
release“. Diese Datei ist die Release-Dokumentation, keine zweite Aufgabenliste.
Board-Scope und Ticketstatus bleiben maßgeblich.

Aktueller Stand vom 4. Oktober 2026: Der integrierte Mainstand wird als reguläre
persönliche Testversion **v0.9.0** geführt, siehe [Changelog](../CHANGELOG.md).
Die Versionsquelle folgt der bisherigen Runtime-Version 0.8.0. Dieser alte
v0.0.1-Veröffentlichungsentwurf bestimmt weder die aktuelle Testversion noch einen
Tag oder eine Veröffentlichung; die folgenden Entwurfsangaben bleiben historisch.

## Versionszuordnung

`v0.0.1` bezeichnet die geplante erste Git-Release-Version. Vorhandene Angaben
„Konzept V0.7“ und „MVP 0.8“ bezeichnen interne Dokument-/Implementierungsstände.
Sie werden nicht umbenannt und nicht als frühere veröffentlichte Releases
interpretiert. Diese Vorbereitung setzt keine neue Runtime-Versionskonstante.

## Entwurf der Release-Beschreibung

Admin in the Loop ist ein früher, menschlich gesteuerter MVP für AI-gestützte
Terminalarbeit. Command-Vorschläge werden lokal geprüft und freigegeben;
ausgewählte Terminalausgabe wird in einem getrennten Review für die AI
freigegeben. Die AI erhält über die Bridge keine Shell-/Dateiwerkzeuge oder
Freigabeaktion.

Der Quellstand umfasst den einzelnen Python-Anwendungseinstieg mit Controller,
GUI, PTY-Helfer, Netzwerkclient und Bridge-only-Modus, die lokalen Testdateien,
Betriebs-/Konzeptdokumentation und Branding-Assets. Ein einzelner Einstieg
bedeutet nicht einen einzigen Prozess oder eine paketfreie Netzwerkanwendung.
Der offizielle OpenAI-Tunnel-Client ist separat und nicht Teil der
Python-Anwendungsdatei.

Einstieg und Voraussetzungen: [Root-README](../README.md).
Verbindungsaufbau: [lokale Bridge und OpenAI-Tunnel](../tests/Local_Integration_README.md#lokale-bridge-und-openai-tunnel).
Branding: [Asset-Guide](assets/branding/README.md).
Lizenz: [vorhandene MIT License](../LICENSE).

### Nachweise und Grenzen

Der [historische Testbericht](../tests/MVP_0.8_TEST_REPORT.md) dokumentiert 86
bestandene Tests am 30. September 2026 in einer Ubuntu-/Linux-Umgebung. Das ist
keine aktuelle v0.0.1-Abnahme. Sein echter Tunnel-Client wurde gegen eine lokale
simulierte Kontrollplattform geprüft, nicht gegen einen echten ChatGPT-Tunnel.

Native macOS-/Terminal.app- und praktische Debian-Desktop-Abnahmen sowie ein
echter ChatGPT-Round-Trip sind durch diesen Bericht nicht belegt. Ebenso wenig
belegt er die Isolation eines konkreten Qubes-/Gateway-Betriebsprofils.
Die aktuellen Nachweise müssen dem später tatsächlich ausgewählten
Release-Quellstand zugeordnet werden.

Menschliche Freigabe verhindert keine schädlichen Commands oder Prompt
Injection. Keine automatische Secret-Redaction, keine verifizierten
Commandgrenzen/Exitcodes, keine starke Isolation bei gleicher Benutzerkennung.
Weitere Grenzen: [Sicherheitsmodell](../tests/Local_Integration_README.md#sicherheitsmodell-und-präzisierte-grenzen).

## Übergabe an die spätere Veröffentlichung

Noch kein Release-Commit, Tag, Download-URL oder veröffentlichtes Release-Artefakt
ist durch diesen Entwurf festgelegt. Der spätere Operator muss den tatsächlichen
Quellstand und die zugehörigen Prüfnachweise übernehmen und die endgültige
Release-Beschreibung darauf abstimmen. Veröffentlichungsdetails werden nicht
aus dem Task-Titel abgeleitet.

Das README ist lokal vorbereitet; die tatsächliche GitHub-Darstellung nach der
späteren Veröffentlichung ist noch zu prüfen. Die App-UI-Logo-Integration ist
eine separate Folgeaufgabe und gehört nicht zu diesem Dokumentationspass.
