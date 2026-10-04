# Changelog

## 0.9.0 — 2026-10-04

Reguläre persönliche Testversion, lokal auf Main festgeschrieben; nicht
veröffentlicht und nicht getaggt. Basis ist der unabhängig geprüfte, kombinierte
Integrationsstand `6108390d927c9247fdf4627ef0bc2a1b345d901c`.

- Kurzer, vorlesbarer Chat-Code mit acht Zufallszeichen, getrennter Conversation-ID,
  24-h-Gültigkeit, persistentem Versuchsschutz, Rotation und permanenter AI-Anzeige.
- Generationgebundene Events/ACKs sowie atomare Limiterwrites und fail-closed
  Korruptionsprüfung; F1–F4 unabhängig geschlossen und lokal kombiniert geprüft.
- Konsolidierte stdlib-Bridge mit approved-output Events und kanonischem Pull-Fallback.
- Optionales lokales sh-guard-Plumbing einschließlich Review-UI integriert;
  standardmäßig OFF, alle realen Pins weiterhin `approved=False`.

Operatorentscheidung vom 4. Oktober 2026, 23:06 Europe/Berlin: Der alleinige Tester
nimmt diesen Stand für persönliche Tests an; externe Vorab-Abnahmen sind kein
Versionsgate. Hosted-/Tunnelbinary-/native Prüfungen bleiben NOT_RUN beziehungsweise
separate Skips. Die Mac-Vorschau `cff53d8804935f7d58337264b00b5ebb6b0495e6` ist nicht
integriert. Keine Produktions-Pinfreigabe oder Änderung menschlicher Approvalgrenzen.
