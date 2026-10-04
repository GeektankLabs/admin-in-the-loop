"""Tk/xterm smoke test under a virtual display; no external Python test library.
Run: xvfb-run -a python3 tests/gui_smoke_test.py
This is not a macOS or manual desktop acceptance test.
"""
import os
import json
import shutil
import subprocess
import gc
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr
import io

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a
from test_connected_mvp import mcp, RemoteFixture
from test_local_setup import fake_brew


class GUISmokeTests(unittest.TestCase):
    def test_native_tk_xterm_review_and_connection_dialog(self):
        with tempfile.TemporaryDirectory(prefix="aitl-gui-") as directory:
            preferences = Path(directory, "config")
            core = a.AppCore(a.PreferenceStore(preferences), a.ConnectionStore(preferences))
            root = tk.Tk()
            core.start_local(directory)
            ui = a.ReviewUI(root, core)
            original = ui.messagebox.showerror
            failures = []
            ui.messagebox.showerror = lambda *args, **kwargs: failures.append("gui_error")
            try:
                root.update()
                self.assertEqual(ui.editor.text.cget("wrap"), "word")
                self.assertGreater(ui.editor.frame.winfo_width(), ui.readiness.winfo_width())
                self.assertLessEqual(abs(ui.from_terminal_button.winfo_rooty() - ui.from_ai_button.winfo_rooty()), 1)
                self.assertLessEqual(abs(ui.from_ai_button.winfo_rooty() - ui.approve_button.winfo_rooty()), 1)
                self.assertEqual(ui.preference.get(), "SMALL")
                self.assertEqual(core.output_mode, "LAST_20")
                self.assertEqual(ui.output_mode.get(), "Letzte 20")
                self.assertTrue(ui.terminal_button.winfo_ismapped())
                ui.output_mode.set("Letzte 50")
                ui.change_output_mode()
                self.assertEqual(core.output_mode, "LAST_50")
                ui.output_mode.set("Letzte 20")
                ui.change_output_mode()
                self.assertEqual(core.output_mode, "LAST_20")
                ui.open_terminal()
                def until(predicate, timeout=8):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        root.update()
                        if predicate():
                            return
                        time.sleep(0.01)
                    raise a.ProbeError("gui_smoke_timeout")
                until(lambda: core.connection == "CONNECTED")
                self.assertFalse(ui.terminal_button.winfo_ismapped())
                self.assertIn("Terminal verbunden", ui.terminal_status.get())
                ui.action(lambda: core.add_proposal("printf 'Hello Admin in the Loop\\n'"))
                ui.from_ai_button.invoke()
                root.update()
                self.assertEqual(core.review_state, "REVIEW")
                self.assertTrue(ui.return_row.winfo_ismapped())
                self.assertTrue(ui.final_return.get())
                with mock.patch.object(ui.messagebox, "askyesno", return_value=True):
                    ui.approve_button.invoke()
                    until(lambda: core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
                    if core.review_state == "OVERRIDE_REQUIRED":
                        ui.approve_button.invoke()
                        until(lambda: core.review_state == "TRANSFERRED")
                until(lambda: core.output_mode == "SINCE_LAST" and b"Hello Admin in the Loop\r\n" in core.output.data)
                ui.from_terminal_button.invoke()
                root.update()
                self.assertFalse(ui.return_row.winfo_ismapped())
                self.assertEqual(core.review.direction, "TO_OUTPUT_SINK")
                ui.reject_button.invoke()
                ui.connection_settings()
                root.update()
                self.assertTrue(any(isinstance(widget, tk.Toplevel) for widget in root.winfo_children()))
                dialog = ui.connection_dialog
                remote_command = dialog.remote_command_box.text.get("1.0", "end-1c")
                self.assertIn("admin_in_the_loop.py --bridge", remote_command)
                self.assertIn("--tls-name bridge.example", remote_command)
                self.assertIsNone(core.bridge_process)
                core.connect_local_bridge()  # Exercise the transport smoke without real Platform credentials.
                until(lambda: core.bridge_state == "CONNECTED")
                until(lambda: core.chat_binding is not None)
                self.assertIn("Lokale Bridge verbunden", dialog.summary.get())
                shown = core.chat_binding["code"][:4] + "-" + core.chat_binding["code"][4:]
                self.assertEqual(ui.chat_code.get(), shown)
                self.assertTrue(ui.chat_code_copy.instate(["!disabled"]))
                ui.chat_code_copy.invoke()
                self.assertEqual(root.clipboard_get(), shown)
                dialog.buttons["chat_show"].invoke()
                self.assertEqual(dialog.binding.get(), shown)
                dialog.buttons["chat_copy"].invoke()
                self.assertEqual(root.clipboard_get(), shown)
                mcp(core.bridge_process, "submit_proposal", {"request_id": "GUI_SMOKE_REQUEST_0001", "text": "printf 'GUI_MCP\\n'"})
                until(lambda: bool(core.inbox))
                ui.from_ai_button.invoke()
                root.update()
                self.assertEqual(core.review_state, "REVIEW")
                self.assertTrue(core.review.proposal_id)
                self.assertEqual(ui.editor.text.cget("state"), "disabled")
                # An output OSC 52 must not silently replace this isolated X clipboard.
                root.clipboard_clear()
                root.clipboard_append("GUI_CLIPBOARD_CANARY")
                root.update()
                core.reject(core.review.id)
                review = core.review_command("printf '\\033]52;c;UkVQTEFDRUQ=\\007'; printf 'OSC52_DONE\\n'", True)
                core.approve(review.id)
                until(lambda: core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"})
                if core.review_state == "OVERRIDE_REQUIRED":
                    core.approve(review.id, True)
                until(lambda: b"OSC52_DONE\r\n" in core.output.data)
                root.update()
                self.assertEqual(root.clipboard_get(), "GUI_CLIPBOARD_CANARY")
                self.assertEqual(failures, [])
            finally:
                ui.messagebox.showerror = original
                # Unlike normal app-close, deliberately end the isolated test terminal.
                if core.channel:
                    try:
                        review = core.review_command("exit", True)
                        core.approve(review.id)
                        until(lambda: core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED"}, 2)
                        if core.review_state == "OVERRIDE_REQUIRED":
                            core.approve(review.id, True)
                    except a.ProbeError:
                        pass
                core.close()
                root.destroy()
                if ui.launcher:
                    try:
                        ui.launcher.wait(timeout=2)
                    except Exception:
                        ui.launcher.terminate()


class ConnectionDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aitl-setup-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.core = a.AppCore(connection_store=a.ConnectionStore(Path(self.tmp.name, "config")))
        self.root = tk.Tk()
        self.callbacks = []
        self.root.report_callback_exception = lambda *_: self.callbacks.append("tk_callback_failed")
        self.ui = a.ReviewUI(self.root, self.core)
        self.addCleanup(self.cleanup)
        self.root.update()

    def cleanup(self):
        self.core.close()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.ui = self.core = self.root = None
        gc.collect()  # Dispose Tcl-owned cycles on the creating thread, before another worker starts.

    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("gui_setup_timeout")

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_panel_proportions_and_shared_footer_at_supported_sizes(self):
        for width in (960, 1360):
            self.root.geometry("%dx600" % width)
            self.root.update()
            widths = [panel.winfo_width() for panel in self.ui.panels]
            for actual, target in zip(widths, (0.25, 0.5, 0.25)):
                self.assertAlmostEqual(actual / sum(widths), target, delta=0.03)
            ys = [button.winfo_rooty() for button in
                  (self.ui.from_terminal_button, self.ui.approve_button, self.ui.from_ai_button)]
            self.assertLessEqual(max(ys) - min(ys), 1)
            headers = [next(widget for widget in label.master.winfo_children()
                            if widget.winfo_class() == "TButton") for label, _ in self.ui.status_labels]
            self.assertLessEqual(abs(headers[0].winfo_rooty() - headers[1].winfo_rooty()), 1)
            self.assertGreater(int(self.ui.readiness.cget("wraplength")), widths[2] - 60)
            for section, panel in (("terminal", self.ui.panels[0]), ("ai", self.ui.panels[2])):
                image = self.ui.section_images[section]
                self.assertEqual((image.width(), image.height()), (80, 64))
                graphic = self.ui.section_logo_labels[section]
                self.assertTrue(graphic.winfo_ismapped())
                self.assertIs(graphic.master.master.master, panel)
            self.assertGreater(self.ui.header_bar.winfo_width() - self.ui.logo_label.winfo_width(), 600)
            self.assertGreater(self.ui.layout.winfo_rooty(),
                               self.ui.header_bar.winfo_rooty() + self.ui.header_bar.winfo_height() - 1)
        base = ttk.Style(self.root).lookup("TFrame", "background") or self.root.cget("background")
        self.assertEqual(self.ui.logo_variant, a._logo_variant(self.root, base))
        left, center, right = self.ui.panels
        self.assertEqual(left.cget("highlightbackground"), right.cget("highlightbackground"))
        self.assertNotEqual(left.cget("highlightbackground"), center.cget("highlightbackground"))
        self.assertTrue(any(getattr(widget, "cget", lambda _: "")("text") == "Review"
                            for widget in self.descendants(center) if widget.winfo_class() == "TLabel"))
        self.assertEqual(self.ui.editor.text.cget("state"), "disabled")
        self.assertEqual(self.callbacks, [])

    def test_logo_variant_follows_light_and_dark_native_surfaces(self):
        class Surface:
            def __init__(self, rgb):
                self.rgb = rgb

            def winfo_rgb(self, _background):
                return self.rgb

        self.assertEqual(a._logo_variant(Surface((65535, 65535, 65535)), "light"), "light")
        self.assertEqual(a._logo_variant(Surface((5000, 5000, 5000)), "dark"), "dark")

    def test_hand_buttons_preserve_review_actions_and_confirmation(self):
        self.assertTrue(self.ui.approve_button.instate(["disabled"]))
        self.assertTrue(self.ui.reject_button.instate(["disabled"]))
        self.core.session = "HAND_BUTTON_TEST_SESSION"
        self.core.connection = "CONNECTED"
        review = self.core._output_review("selected output\n")
        self.ui.render()
        self.assertEqual(self.ui.approve_button.cget("text"), "Freigeben an AI →")
        self.ui.approve_button.invoke()
        self.assertEqual(self.core.review_state, "ACKNOWLEDGED_LOCAL")
        self.assertEqual(list(self.core.approved_outputs), [review])

        self.core._output_review("do not share\n")
        self.ui.render()
        self.ui.reject_button.invoke()
        self.assertEqual(self.core.review_state, "REJECTED")
        self.assertEqual(list(self.core.approved_outputs), [review])
        self.assertEqual(self.ui.reject_button.cget("text"), "Verwerfen")

        command = self.core.review_command("printf test", False)
        self.core.channel = mock.Mock()
        self.core.review_state = "OVERRIDE_REQUIRED"
        self.ui.render()
        with mock.patch.object(self.ui.messagebox, "askyesno", return_value=False):
            self.ui.approve_button.invoke()
        self.core.channel.send.assert_not_called()
        with mock.patch.object(self.ui.messagebox, "askyesno", return_value=True):
            self.ui.approve_button.invoke()
        self.core.channel.send.assert_called_once_with({"type": "transfer", "id": command.id,
            "session": command.session, "text": command.text, "return": False, "override": True})
        self.assertEqual(self.core.review_state, "CHECKING")
        self.core.channel = None
        self.assertEqual(self.callbacks, [])

    def test_centered_dropdowns_keep_selection_callbacks(self):
        self.assertEqual(str(self.ui.output_picker.cget("justify")), "center")
        self.ui.output_picker.set("Letzte 50")
        self.ui.output_picker.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(self.core.output_mode, "LAST_50")
        picker = next(widget for widget in self.descendants(self.ui.panels[2])
                      if isinstance(widget, ttk.Combobox))
        self.assertEqual(str(picker.cget("justify")), "center")
        picker.set("LARGE")
        picker.event_generate("<<ComboboxSelected>>")
        self.root.update()
        self.assertEqual(self.core.preferences, "LARGE")
        self.assertEqual(self.callbacks, [])

    def test_one_window_contextual_sections_drafts_and_keyboard_scroll(self):
        dialog = self.ui.connection_settings()
        self.root.update()
        self.assertIs(self.ui.connection_settings(), dialog)
        self.assertFalse(any(widget.winfo_class() == "TNotebook" for widget in self.descendants(dialog.window)))
        with mock.patch.object(self.core, "connect_local_bridge") as local, \
                mock.patch.object(self.core, "begin_pair_remote") as remote:
            dialog.location.set("REMOTE")
            dialog.fields["remote_script"].set("/opt/Remote Script/admin_in_the_loop.py")
            dialog.fields["code"].set("KEEP-DRAF-T123")
            self.root.update()
            self.assertTrue(dialog.remote.winfo_ismapped())
            self.assertFalse(dialog.local_tunnel.winfo_ismapped())
            self.assertIn("'/opt/Remote Script/admin_in_the_loop.py' --bridge", dialog.remote_command)
            dialog.location.set("LOCAL")
            dialog.location.set("REMOTE")
            self.assertEqual(dialog.fields["code"].get(), "KEEP-DRAF-T123")
            local.assert_not_called()
            remote.assert_not_called()
        dialog.window.geometry("640x480")
        dialog.details.set(True)
        dialog.select_location()
        self.root.update()
        dialog.buttons["offline"].focus_force()
        self.root.update()
        self.assertGreater(dialog.canvas.yview()[0], 0)
        top = dialog.buttons["offline"].winfo_rooty()
        self.assertGreaterEqual(top, dialog.canvas.winfo_rooty())
        self.assertLessEqual(top + dialog.buttons["offline"].winfo_height(),
                             dialog.canvas.winfo_rooty() + dialog.canvas.winfo_height())
        dialog.fields["url"].set("http://bridge.example:8443")
        self.assertTrue(dialog.buttons["remote_copy"].instate(["disabled"]))
        self.assertEqual(self.callbacks, [])

    def test_pair_validation_does_not_submit_or_discard_bad_draft(self):
        dialog = self.ui.connection_settings()
        dialog.location.set("REMOTE")
        dialog.fields["pin"].set("a" * 64)
        dialog.fields["code"].set("ABCD-EFGH-JKMP")
        with mock.patch.object(self.core, "begin_pair_remote") as pair:
            dialog.run("bridge", dialog.pair)
            pair.assert_not_called()
            self.assertIn("unabhängig", dialog.errors["bridge"].get())
            self.assertEqual(dialog.fields["code"].get(), "ABCD-EFGH-JKMP")
            dialog.compared.set(True)
            dialog.fields["pin"].set("b" * 64)
            self.assertFalse(dialog.compared.get())
            dialog.compared.set(True)
            dialog.fields["url"].set("https://user:secret@bridge.example:8443")
            self.assertFalse(dialog.compared.get())
            dialog.compared.set(True)
            dialog.run("bridge", dialog.pair)
            pair.assert_not_called()
            self.assertNotIn("secret", dialog.errors["bridge"].get())
        self.assertEqual(dialog.entries["code"].cget("show"), "*")
        self.assertEqual(dialog.entries["api_key"].cget("show"), "*")
        self.assertEqual(self.callbacks, [])

    def test_real_remote_pair_completion_and_saved_target_guard(self):
        bridge = RemoteFixture(Path(self.tmp.name, "bridge"))
        self.addCleanup(bridge.close)
        dialog = self.ui.connection_settings()
        dialog.location.set("REMOTE")
        dialog.fields["url"].set(bridge.url)
        dialog.fields["pin"].set(bridge.pin)
        dialog.fields["code"].set(bridge.code)
        dialog.compared.set(True)
        with mock.patch.object(self.core, "connect_local_bridge") as local, \
                mock.patch.object(self.core, "start_tunnel") as tunnel:
            dialog.buttons["pair"].invoke()
            self.assertEqual(dialog.fields["code"].get(), "")
            self.assertTrue(dialog.buttons["disconnect"].instate(["disabled"]))
            dialog.window.destroy()
            self.until(lambda: self.core.bridge_state == "CONNECTED")
            dialog = self.ui.connection_settings()
            self.root.update()
            self.assertIn("Remote-Bridge verbunden · authentifiziert", dialog.summary.get())
            self.assertIn("Tunnelbetrieb am Gateway", self.ui.ai_status.get())
            self.assertIn("noch nicht verifiziert", dialog.summary.get())
            self.assertNotIn("Tunnel gestoppt", self.ui.ai_status.get())
            self.assertIsNone(self.core.bridge_process)
            self.assertIsNone(self.core.tunnel)
            local.assert_not_called()
            tunnel.assert_not_called()
        self.assertEqual(dialog.binding.get(), "")
        self.until(lambda: self.core.chat_binding is not None)
        dialog.buttons["chat_show"].invoke()
        shown = self.core.chat_binding["code"][:4] + "-" + self.core.chat_binding["code"][4:]
        self.assertEqual(dialog.binding.get(), shown)
        self.assertTrue(self.ui.chat_code_copy.instate(["!disabled"]))
        self.ui.chat_code_copy.invoke()
        self.assertEqual(self.root.clipboard_get(), shown)
        original_binding = dict(self.core.chat_binding)
        self.core.chat_binding = dict(original_binding, status="BACKOFF",
                                      retry_at=original_binding["server_time"] + 45,
                                      restriction_scope="PRINCIPAL")
        self.ui.render()
        self.assertEqual(self.ui.chat_code.get(), shown)
        self.assertIn("Wartezeit", self.ui.chat_code_status.get())
        self.assertTrue(self.ui.chat_code_copy.instate(["disabled"]))
        self.core.chat_binding = original_binding
        self.ui.render()
        old_code = original_binding["code"]
        dialog.buttons["chat_rotate"].invoke()
        self.until(lambda: self.core.chat_binding and self.core.chat_binding["generation"] == 2)
        self.root.update()
        self.assertNotEqual(self.core.chat_binding["code"], old_code)
        self.assertEqual(self.ui.chat_code.get(), self.core.chat_binding["code"][:4] + "-" +
                         self.core.chat_binding["code"][4:])
        self.assertEqual(sum(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()), 1)
        self.core.disconnect_bridge()
        self.ui.render()
        self.assertEqual(dialog.binding.get(), "")
        dialog.fields["url"].set("https://unrelated.example:8443")
        self.assertTrue(dialog.buttons["saved_connect"].instate(["disabled"]))
        with mock.patch.object(self.core, "connect_saved_bridge") as connect:
            dialog.run("bridge", dialog.open_saved)
            connect.assert_not_called()
        self.assertIn("passt nicht", dialog.errors["bridge"].get())
        self.assertEqual(self.callbacks, [])

    def test_local_two_value_flow_deferred_save_progress_and_conditional_actions(self):
        dialog = self.ui.connection_settings()
        self.assertNotIn("binary", dialog.entries)
        self.assertNotIn("local_connect", dialog.buttons)
        self.assertNotIn("tunnel_save", dialog.buttons)
        self.assertFalse(dialog.bridge_section.winfo_ismapped())
        self.assertFalse(dialog.buttons["local_stop"].winfo_ismapped())
        self.assertTrue(dialog.buttons["local_start"].instate(["!disabled"]))
        dialog.fields["tunnel_id"].set("tunnel_" + "a" * 32)
        dialog.fields["api_key"].set("SYNTHETIC_UI_RUNTIME_KEY")
        executable = Path(self.tmp.name, "client")
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        preparation = mock.Mock(events=a.queue.Queue())
        client = mock.Mock(state="READY", error=None)
        client.polling_check.return_value = (False, None)
        with mock.patch.object(a, "LocalSetupPreparation", return_value=preparation), \
                mock.patch.object(a, "TunnelProcess", return_value=client):
            dialog.buttons["local_start"].invoke()
            self.assertIn("gesucht", dialog.summary.get())
            self.assertTrue(dialog.buttons["local_start"].instate(["disabled"]))
            self.assertIsNone(self.core.connection_store.load_connection())
            preparation.events.put(("stage", "INSTALLING"))
            self.core.pump()
            self.ui.render()
            self.assertIn("installiert", dialog.summary.get())
            process = a.LocalBridgeProcess()
            config = {"binary": str(executable), "tunnel_id": dialog.fields["tunnel_id"].get(),
                      "api_key": dialog.fields["api_key"].get()}
            preparation.events.put(("prepared", (process, config)))
            self.until(lambda: self.core.tunnel is client)
            self.assertIsNone(self.core.connection_store.load_connection())
            self.assertIn("geprüft", dialog.summary.get())
            dialog.window.destroy()
            client.polling_check.return_value = (True, None)
            self.core.local_check_at = 0
            self.until(lambda: self.core.local_setup_state == "CONNECTED")
            dialog = self.ui.connection_settings()
            self.root.update()
            self.assertEqual(self.core.connection_store.load_connection()["tunnel"], config)
            self.assertIn("Verbindung bereit", dialog.summary.get())
            self.assertIn("manuell prüfen", dialog.summary.get())
            self.assertFalse(dialog.local_inputs.winfo_ismapped())
            self.assertFalse(dialog.buttons["local_start"].winfo_ismapped())
            self.assertTrue(dialog.buttons["local_stop"].winfo_ismapped())
            dialog.buttons["local_stop"].invoke()
            self.assertIsNone(self.core.tunnel)
            self.assertIsNone(self.core.bridge_process)
            self.root.update()
            self.assertFalse(dialog.buttons["local_stop"].winfo_ismapped())
            self.assertTrue(dialog.buttons["local_start"].winfo_ismapped())
            client.close.assert_called_once()
        self.assertEqual(self.callbacks, [])

    def test_local_preflight_failure_help_and_reopened_installation_are_truthful(self):
        dialog = self.ui.connection_settings()
        dialog.fields["tunnel_id"].set("bad-id")
        dialog.fields["api_key"].set("SYNTHETIC_SECRET_KEY")
        with mock.patch.object(a, "LocalSetupPreparation") as prepare:
            dialog.buttons["local_start"].invoke()
            prepare.assert_not_called()
        self.assertIn("32 kleinen Hexzeichen", dialog.errors["tunnel"].get())
        self.assertNotIn("SYNTHETIC_SECRET_KEY", dialog.errors["tunnel"].get())
        self.assertIsNone(self.core.connection_store.load_connection())
        with mock.patch("webbrowser.open", return_value=True) as browser:
            dialog.buttons["tunnel_help"].invoke()
            dialog.buttons["key_help"].invoke()
            self.assertEqual(browser.call_args_list, [mock.call(a.TUNNEL_SETTINGS_URL), mock.call(a.RUNTIME_KEYS_URL)])
        dialog.fields["tunnel_id"].set("tunnel_" + "a" * 32)
        preparation = mock.Mock(events=a.queue.Queue())
        with mock.patch.object(a, "LocalSetupPreparation", return_value=preparation):
            dialog.buttons["local_start"].invoke()
            preparation.events.put(("stage", "INSTALLING"))
            self.core.pump()
            dialog.window.destroy()
            dialog = self.ui.connection_settings()
            self.assertEqual(dialog.location.get(), "LOCAL")
            self.assertIn("installiert", dialog.summary.get())
            self.assertTrue(dialog.buttons["local_start"].instate(["disabled"]))
            self.assertFalse(dialog.buttons["local_stop"].winfo_ismapped())
            preparation.events.put(("error", "runtime_key_rejected"))
            self.core.pump()
            self.ui.render()
            self.assertIn("401", dialog.errors["tunnel"].get())
            self.assertIsNone(self.core.connection_store.load_connection())
            self.assertTrue(dialog.buttons["local_start"].instate(["!disabled"]))
        self.assertEqual(self.callbacks, [])

    def test_failed_macos_installer_has_gui_and_console_details_without_secret(self):
        secret = "SYNTHETIC_MAC_INSTALL_SECRET"
        brew = fake_brew(Path(self.tmp.name), exit_code=17,
                         output="Error: Permission denied\nTOKEN=" + secret)
        dialog = self.ui.connection_settings()
        dialog.fields["tunnel_id"].set("tunnel_" + "a" * 32)
        dialog.fields["api_key"].set(secret)
        with mock.patch.object(a.sys, "platform", "darwin"), \
                mock.patch.object(a.shutil, "which", side_effect=lambda name: str(brew) if name == "brew" else None), \
                redirect_stderr(io.StringIO()) as console:
            dialog.buttons["local_start"].invoke()
            self.until(lambda: self.core.local_setup_state == "FAILED")
        self.root.update()
        self.assertTrue(dialog.cause_label.winfo_ismapped())
        self.assertIn("beschreiben", dialog.failure_cause.get())
        self.assertTrue(dialog.technical_toggle.winfo_ismapped())
        dialog.technical_toggle.invoke()
        self.root.update()
        self.assertTrue(dialog.technical_details.frame.winfo_ismapped())
        text = dialog.technical_details.text.get("1.0", "end-1c")
        self.assertIn("Phase: INSTALLING", text)
        self.assertIn("Exit-Code: 17", text)
        self.assertIn("Permission denied", text)
        self.assertIn("Cellar/tunnel-client", text)
        self.assertIn("Phase: INSTALLING", console.getvalue())
        self.assertIn("Exit-Code: 17", console.getvalue())
        self.assertNotIn(secret, text + console.getvalue() + dialog.failure_cause.get())
        self.assertIsNone(self.core.connection_store.load_connection())
        dialog.window.destroy()
        reopened = self.ui.connection_settings()
        self.assertIn("beschreiben", reopened.failure_cause.get())
        self.assertEqual(self.callbacks, [])

    def test_closed_dialog_recovers_saved_confirm_without_reclaim(self):
        bridge = RemoteFixture(Path(self.tmp.name, "bridge"))
        self.addCleanup(bridge.close)
        dialog = self.ui.connection_settings()
        dialog.location.set("REMOTE")
        dialog.fields["url"].set(bridge.url)
        dialog.fields["pin"].set(bridge.pin)
        dialog.fields["code"].set(bridge.code)
        dialog.compared.set(True)
        original = a.BridgeHTTP.request
        claims = []
        lost = False
        def request(client, method, path, *args, **kwargs):
            nonlocal lost
            if path == "/v1/pair/claim":
                claims.append("claim")
            result = original(client, method, path, *args, **kwargs)
            if path == "/v1/pair/confirm" and not lost:
                lost = True
                raise a.TransportError("network_unavailable", True)
            return result
        with mock.patch.object(a.BridgeHTTP, "request", request):
            dialog.buttons["pair"].invoke()
            dialog.window.destroy()
            self.until(lambda: self.core.bridge_state == "BLOCKED" and not self.core.pair_thread.is_alive())
            self.assertIsNone(self.core.saved_connection)
            dialog = self.ui.connection_settings()
            self.assertEqual(dialog.fields["url"].get(), bridge.url)
            self.assertEqual(dialog.fields["pin"].get(), bridge.pin)
            self.assertTrue(dialog.buttons["saved_connect"].instate(["!disabled"]))
            dialog.buttons["saved_connect"].invoke()
            self.until(lambda: self.core.bridge_state == "CONNECTED")
            self.assertEqual(len(claims), 1)
            self.assertIsNone(self.core.bridge_process)
        self.assertEqual(self.callbacks, [])


class BrandingSingleFileTests(unittest.TestCase):
    def test_downloaded_single_file_renders_both_variants_from_external_cwd(self):
        source = Path(a.__file__).resolve()
        with tempfile.TemporaryDirectory(prefix="aitl-single-file-") as directory:
            sandbox = Path(directory).resolve()
            self.assertFalse(sandbox.is_relative_to(source.parent))
            download = sandbox / "Downloads"
            working = sandbox / "external-cwd"
            download.mkdir()
            working.mkdir()
            shutil.copyfile(source, download / source.name)
            code = r'''
import gc, json, sys, tkinter as tk
from tkinter import ttk
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import admin_in_the_loop as app
assert Path(app.__file__).parent != Path.cwd()
assert sorted(p.name for p in Path(app.__file__).parent.iterdir()) == ["admin_in_the_loop.py"]
results = []
def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)

for variant, background in (("light", "#ffffff"), ("dark", "#101010")):
    root = tk.Tk()
    ttk.Style(root).configure("TFrame", background=background)
    core = app.AppCore()
    ui = app.ReviewUI(root, core)
    root.update()
    assert ui.logo_variant == variant
    for section, panel in (("terminal", ui.panels[0]), ("ai", ui.panels[2])):
        image = ui.section_images[section]
        graphic = ui.section_logo_labels[section]
        assert image is not None
        assert (image.width(), image.height()) == (80, 64)
        assert graphic.winfo_ismapped() and str(graphic.cget("image"))
        assert graphic.master.master.master is panel
        assert graphic.winfo_rooty() < ui.status_labels[0 if section == "terminal" else 1][0].winfo_rooty()
        assert image.transparency_get(0, 0) == (variant == "dark")
    assert ui.logo_label.cget("text") == "Admin in the Loop"
    for name, button in (("ok", ui.approve_button), ("stop", ui.reject_button)):
        image = ui.button_hand_images[name]
        assert image is not None and (image.width(), image.height()) == (24, 24)
        assert image.transparency_get(0, 0)
        assert tuple(map(str, root.tk.splitlist(button.cget("image")))) == (str(image),)
        assert str(button.cget("compound")) == "left" and button.winfo_ismapped()
        assert button.instate(["disabled"])
    assert ui.header_bar.winfo_width() - ui.logo_label.winfo_width() > 600
    for width in (960, 1100, 1360):
        root.geometry("%dx600" % width)
        root.update()
        for section, panel in (("terminal", ui.panels[0]), ("ai", ui.panels[2])):
            center = panel.winfo_rootx() + panel.winfo_width() / 2
            graphic = ui.section_logo_labels[section]
            assert abs(graphic.winfo_rootx() + graphic.winfo_width() / 2 - center) <= 1
            for widget in descendants(panel):
                if isinstance(widget, ttk.Label) and not str(widget.cget("image")):
                    assert str(widget.cget("anchor")) == "center"
                    assert str(widget.cget("justify")) == "center"
                    assert abs(widget.winfo_rootx() + widget.winfo_width() / 2 - center) <= 1
                if isinstance(widget, ttk.Combobox):
                    assert str(widget.cget("justify")) == "center"
                    assert widget.winfo_width() >= panel.winfo_width() - 30
        for label, _ in ui.status_labels:
            assert str(label.cget("justify")) == "center"
        review_title = next(w for w in descendants(ui.panels[1])
                            if isinstance(w, ttk.Label) and w.cget("text") == "Review")
        assert review_title.pack_info()["anchor"] == "w"
        assert ui.editor.text.cget("state") == "disabled"
    assert core.bridge_process is None and core.tunnel is None
    results.append({"variant": variant, "dimensions": [80, 64], "sections": ["terminal", "ai"], "mapped": True})
    core.close()
    for callback in root.tk.call("after", "info"):
        root.after_cancel(callback)
    root.destroy()
    del ui, core, root
    gc.collect()
assert sorted(p.name for p in Path(sys.argv[1]).iterdir()) == ["admin_in_the_loop.py"]
assert not list(Path.cwd().iterdir())
print(json.dumps(results))
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(download)], cwd=working,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [
                {"variant": "light", "dimensions": [80, 64], "sections": ["terminal", "ai"], "mapped": True},
                {"variant": "dark", "dimensions": [80, 64], "sections": ["terminal", "ai"], "mapped": True},
            ])

    def test_embedded_png_decode_failure_keeps_text_fallback(self):
        root = tk.Tk()
        core = a.AppCore()
        ui = None
        try:
            with mock.patch.dict(a._SECTION_LOGO_PNG, {"terminal": {"light": "invalid", "dark": "invalid"},
                                                      "ai": {"light": "invalid", "dark": "invalid"}}):
                ui = a.ReviewUI(root, core)
                root.update()
                self.assertEqual(ui.section_images, {"terminal": None, "ai": None})
                self.assertEqual(ui.section_logo_labels, {})
                self.assertEqual(ui.logo_label.cget("text"), "Admin in the Loop")
                self.assertTrue(ui.logo_label.winfo_ismapped())
                for label, _ in ui.status_labels:
                    titles = [child.cget("text") for child in label.master.winfo_children()
                              if child.winfo_class() == "TLabel" and not str(child.cget("textvariable"))]
                    self.assertTrue(any(title in {"Terminal", "AI / MCP"} for title in titles))
        finally:
            core.close()
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()
            del ui, core, root
            gc.collect()

    def test_invalid_hand_icons_keep_text_buttons_and_actions(self):
        root = tk.Tk()
        core = a.AppCore()
        ui = None
        try:
            with mock.patch.dict(a._BUTTON_HAND_PNG, {"ok": "invalid", "stop": "invalid"}):
                ui = a.ReviewUI(root, core)
                root.update()
                self.assertEqual(ui.button_hand_images, {"ok": None, "stop": None})
                self.assertFalse(str(ui.approve_button.cget("image")))
                self.assertFalse(str(ui.reject_button.cget("image")))
                self.assertEqual(ui.reject_button.cget("text"), "Verwerfen")
                core.session = "TEXT_FALLBACK_SESSION"
                core.connection = "CONNECTED"
                core._output_review("reject this\n")
                ui.render()
                ui.reject_button.invoke()
                self.assertEqual(core.review_state, "REJECTED")
        finally:
            core.close()
            for callback in root.tk.call("after", "info"):
                root.after_cancel(callback)
            root.destroy()
            del ui, core, root
            gc.collect()


class TerminalAttachmentUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aitl-terminal-ui-")
        self.core = a.AppCore()
        self.core.start_local(self.tmp.name)
        self.root = tk.Tk()
        self.ui = a.ReviewUI(self.root, self.core)
        self.addCleanup(self.cleanup)

    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("terminal_attach_timeout")

    def end_shell(self):
        if self.core.channel and self.core.connection == "CONNECTED":
            review = self.core.review_command("exit", True)
            self.core.approve(review.id)
            self.until(lambda: self.core.connection == "DISCONNECTED", 3)

    def cleanup(self):
        try:
            self.end_shell()
        except Exception:
            pass
        self.core.close()
        if self.ui.launcher and self.ui.launcher.poll() is None:
            try:
                self.ui.launcher.wait(timeout=2)
            except Exception:
                self.ui.launcher.terminate()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.tmp.cleanup()

    def test_attached_hides_attach_button_and_loss_allows_fresh_attach(self):
        self.root.update()
        button = self.ui.terminal_button
        self.assertTrue(button.winfo_ismapped())
        self.ui.open_terminal()
        self.until(lambda: self.core.connection == "CONNECTED")
        self.ui.render()
        self.assertFalse(button.winfo_ismapped())
        self.assertIn("Terminal verbunden", self.ui.terminal_status.get())

        self.end_shell()
        self.ui.render()
        self.assertTrue(button.winfo_ismapped())
        self.assertEqual(button.instate(["!disabled"]), True)
        self.assertIn("Terminal getrennt", self.ui.terminal_status.get())
        old_endpoint = self.core.endpoint
        button.invoke()
        self.assertIsNot(self.core.endpoint, old_endpoint)
        self.until(lambda: self.core.connection == "CONNECTED")
        self.ui.render()
        self.assertFalse(button.winfo_ismapped())
        self.assertIn("Terminal verbunden", self.ui.terminal_status.get())


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(verbosity=2, resultclass=a.SafeResult))
