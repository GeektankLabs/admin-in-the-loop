Task-ID: task_187995a9-8bfb-5431-8696-a1c0298180e0
# Restart-Save Manifest — 2026-10-10 (worktree-unabhängig, final 11:0xZ)

## Persistenzgrenze (belegt)
- HostRepoRoot `/Users/admin/Documents/github/admin-in-the-loop` bleibt bestehen;
  abgebaut wird laut Nutzerangabe nur der registrierte Worktree
  `.worktrees/i18n` (gitdir-Metadaten `HostRepoRoot/.git/worktrees/i18n`).
  GitCommonDir = `HostRepoRoot/.git` (realpath-bestätigt, gemeinsames Repo).
- Die englischen Commits liegen bereits auf echter lokaler Branchref
  `refs/heads/task/i18n-english-qubes-first` im GitCommonDir — unabhängig vom
  Worktree (Beweis: `git for-each-ref` im Root-Worktree und Fetch aus Bundle in
  ein frisches Repo zeigen dieselbe Ref/denselben HEAD).
- **Lücke:** Die tatsächliche Neustart-/Cleanup-Regel ist aus der VM nicht lesbar
  (kein worktree/cleanup/prune-Eintrag in `.opencode-vm/agent-control.json` und
  Backups; `.opencode-vm/worktrees/` leer). Dass Refs/Dateien im Root den Cleanup
  überleben, ist daher NICHT beweisbar. Das Bundle + beide Archive hier sind die
  unabhängige, worktree-unabhängige Sicherung.

## Artefakte (alle in persistentem Projektroot)
| Datei (relativ zu `planning/restart-saves/2026-10-10-0833/`) | SHA-256 | Umfang |
|---|---|---|
| `aitl-restart-bundle-20261010.bundle` | `fa3a9b6f8088dcd2baa685bc0a474f5ce772e8cf2a4e5b25fc36cbe06f7865a2` | 1640374 B, 10 Refs, vollständige Historie, `git bundle verify` OK |
| `management-before-save.tar.gz` | `d9f2e24ce28b6db8a2930618b292e92ffb66166ac716098c17fd61158749aa7e` | 361501 B, 37 Docs (17 C, 17 P, Englisch-Pause, Reconciliation, Worker-Kontext) |
| `untracked-extra-20261010.tar.gz` | `3a59afc67344000b787d97c60bf3d9ac15a454923fefc50e1db69f80247b7cef` | 5 MCP-Binding-Diagnosedateien (planning/mcp-binding-*) |
| `README.md` | `07a985a3f4c303b22a682d542fe8e98279057efbc00dd8e1d309575734321f63` | Archivzweck |

## Gesicherte Git-Refs (Bundle, verifiziert)
- `refs/heads/main` = `d2b283fed0993c22d5f7c3832f38561ff851934c` (Docscommit)
- `refs/heads/task/i18n-english-qubes-first` = `d6b884394af35f23c424cbd1cf03716b2a661691`
  (enthält `d500ded67102b3040c41449bac8151d38ada22ff`, 5 Dateien)
- `refs/heads/task/task-17334558-chat-binding` = `6d39e9c2a91955651d665591c65949909bbd9d34`
- `refs/heads/task/task-42c788cb-wp-review-ux` = `2d452016ca72b162a1b7cddbf9429fd3596b08d4`
- `refs/heads/task/task-99f021c2-shguard-adapter` = `cff53d8804935f7d58337264b00b5ebb6b0495e6`
- `refs/stash` = `ad0456bc8ac374ed37816f4a858ca2afbf7c3837`
- `refs/remotes/origin/main` = `7a20745e7d61b508d0fef85d1535b56ff9e79be2`

## Bundle-Restore-Lesbarkeit (bewiesen, Prüfrepo wieder entfernt)
Frisches `git init` + `git fetch <bundle> 'refs/heads/*:refs/heads/*' refs/stash`:
alle 6 Objekte `cat-file -t` = commit (d500ded, d6b8843, d2b283f, 73f02fd, ad0456bc
sowie Basis 05dea5a); `d500ded` zeigt 5 Dateien; Blobvergleich
`show d6b8843:planning/pause-i18n-english-qubes-first-2026-10-09.md` →
SHA-256 `29c9ad7015d0cbae9479b1de1884b842780f09b76255d4134a87d2df901dfef5`
= bytegleich zur Worktree-Datei.

## Restore (ohne bestehende Arbeit zu überschreiben)
```bash
cd /Users/admin/Documents/github/admin-in-the-loop
# 1) Refs aus Bundle zurückholen (Namen mit restore-Präfix, kein Zwang):
git fetch planning/restart-saves/2026-10-10-0833/aitl-restart-bundle-20261010.bundle \
  'refs/heads/*:refs/restore/*' 'refs/stash:refs/restore-stash'
# 2) Englisch-Worktree neu anlegen, falls gewünscht (echter Branch existiert wieder):
git worktree add .worktrees/i18n d6b884394af35f23c424cbd1cf03716b2a661691 \
  -b task/i18n-english-qubes-first-restored   # oder ohne -b, wenn Ref schon existiert
# 3) Dokumente/Untracked zurückspielen (nur fehlende Dateien ergänzen):
tar -xzf planning/restart-saves/2026-10-10-0833/management-before-save.tar.gz -C .
tar -xzf planning/restart-saves/2026-10-10-0833/untracked-extra-20261010.tar.gz -C .
```
Alternativ Komplettneuaufbau: `git clone <bundle> <neues Verzeichnis>` (klont alle
Branches/Refs aus dem Bundle).

## Exclusions / Unsicherheiten
- Nicht archiviert (bewusst): `.DS_Store`, `skill/.DS_Store`, `__pycache__/`,
  `tests/__pycache__/` (regenerierbare Artefaktmassen), `.opencode-vm/` Runtime
  (gitignored; Worker-Kontext liegt als Lesekopie im management-tar),
  taskboard.db / agent-control.json (Runtime-/Infrastrukturzustand, keine
  Aufgabe dieser Sicherung).
- Untracked-Fremddokumente (17 C + 17 P anderer Tasks, Reconciliation) sind nur
  im management-tar gesichert, nicht in Git committed (fremde Docs, bestehende
  Grenze). `.worktrees/i18n` selbst enthält keine uncommitteten Dateien
  (tracked-clean, per Status verifiziert).
- Scheduler/Board/Receipts bleiben managementseitig (siehe Restart-Handoff).
