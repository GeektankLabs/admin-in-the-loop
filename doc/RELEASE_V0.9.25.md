# v0.9.25 — UI languages and orange pending review

The authored Tk UI (main window, connection wizard, confirmations, context
menus, analysis details and UI diagnostics) supports German and English.
`LC_ALL`, then `LC_MESSAGES`, then `LANG` selects German for `de` locales;
other locales select English. If these variables are absent, Python's system
locale is used. The existing private preferences file can override this with
`de` or `en`, or follow `system`. Older preference files remain readable.

The language selector saves the choice for the next app start. It does not
rebuild an active review, reconnect, approve, discard or send anything.
Translations apply only to authored display strings. Protocol keys, commands,
terminal/clipboard payloads, analyzer output and diagnostic error codes retain
their original values. The bridge CLI/console and repository documentation are
not part of the translated Tk UI. The catalog stays inside the Python file so
single-file installation and updating still work.

A pending proposal while a command review is open uses light orange `#ffcc99`
with dark text `#713800`. Existing readiness gates and approve/discard actions
are unchanged. Both app and embedded bridge versions are 0.9.25.

## Repository targets verified on 2026-11-10

- Checkout remote `origin`: `https://github.com/geco91/admin-in-the-loop.git`.
  GitHub owner: `geco91`; default and integration branch: `main`.
- GitHub confirms its parent/source as
  `https://github.com/GeektankLabs/admin-in-the-loop.git`, owner `GeektankLabs`,
  default and PR target branch `main`. `upstream/main` was fetched explicitly
  for comparison; the remote name alone was not used to identify ownership.
- Before this change, the fork is 13 commits ahead, zero behind upstream.
  Existing regular [PR #2](https://github.com/GeektankLabs/admin-in-the-loop/pull/2)
  already targets exactly `geco91:main` → `GeektankLabs:main`. Reuse that PR
  when integrating this change, preserving its regular status and earlier
  fork changes. No upstream merge or release tag is part of this task.

## Targeted validation

Python 3.13 / Tk 9 on Linux, with a local Xvfb display at 1920×1080.
The environment lacked a display; the matching Debian Xvfb package was
unpacked under `/tmp` and run as the ordinary user. No system package install,
new CI, test framework or permanent service was added.

The existing German text assertions were run with `LC_ALL=de_DE.UTF-8`:

- `python3 admin_in_the_loop.py --self-test`: 49 passed.
- `python3 tests/test_chat_binding_review_fixes.py`: 8 passed.
- `python3 tests/test_connection_recovery.py`: 20 passed.
- `DISPLAY=:99 python3 tests/test_connection_dialog_ux.py`: 9 passed,
  including four focused additions for locale precedence, preference
  round-trip/compatibility, format placeholders, and real English/German Tk
  labels, selection callbacks, orange gating and preservation of active review.
- `DISPLAY=:99 python3 tests/gui_smoke_test.py` with these existing cases:
  `ConnectionDialogTests.test_pending_proposal_explains_stage_and_transfer_gates`,
  `ConnectionDialogTests.test_panel_proportions_and_shared_footer_at_supported_sizes`,
  `ConnectionDialogTests.test_centered_dropdowns_keep_selection_callbacks`,
  `ConnectionDialogTests.test_ai_proposal_to_clipboard_without_terminal_is_explicit_and_never_transferred`,
  `WorkPackageReviewUITests.test_mouse_context_copy_paste_for_entries_text_and_readonly_fields`,
  `BrandingSingleFileTests.test_downloaded_single_file_renders_both_variants_from_external_cwd`:
  6 passed.
- Python syntax compilation, version output, `git diff --check` passed.
- Main window and connection wizard rendered on the virtual display for visual
  inspection; no production credentials or external terminal targets used.

92 targeted tests passed. This is not the full project suite or native macOS,
Qubes, physical-desktop, real-provider or live ChatGPT end-to-end acceptance.
Remote CI availability and exact published commit are reported separately after
push; local tests do not establish a remote CI result.
