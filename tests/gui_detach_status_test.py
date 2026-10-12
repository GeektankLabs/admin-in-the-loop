"""Isolated Tk test for the L06 terminal detach/end status wording; no xterm or command execution.
Run from repo root: xvfb-run -a -s '-screen 0 1920x1080x24' python3 tests/gui_detach_status_test.py

Covers the L06 transparency change only: the CONNECTED terminal status must state that
closing the app does not end the shell, and the DISCONNECTED status must name both possible
causes instead of claiming one the controller cannot distinguish.
"""
import gc
import sys
import time
import uuid
import tkinter as tk
import unittest
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a

CONNECTED = "Terminal verbunden\nBash · lokale Sitzung\nApp schließen beendet diese Shell nicht"
WAITING = "Nicht verbunden\nTerminal kann geöffnet werden"
STARTING = "Terminal verbindet\nShell startet"
DISCONNECTED = ("Terminal getrennt\nUrsache: Shell beendet oder Verbindung verloren\n"
                "Erneutes Öffnen möglich")


class DetachStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aitl-detach-status-")
        self.core = a.AppCore()
        self.core.start_local(self.tmp.name)  # real state: WAITING_FOR_TERMINAL
        self.root = tk.Tk()
        self.callbacks = []
        self.root.report_callback_exception = lambda *_: self.callbacks.append("tk_callback_failed")
        self.addCleanup(self.cleanup)
        self.ui = a.ReviewUI(self.root, self.core)
        # The terminal status label is the one bound to the terminal_status variable.
        wanted = str(self.ui.terminal_status)
        self.label = next(label for label, _panel in self.ui.status_labels
                          if str(label.cget("textvariable")) == wanted)
        self.root.update()

    def cleanup(self):
        self.core.close()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.tmp.cleanup()
        self.ui = self.label = self.core = self.root = None
        gc.collect()  # Dispose Tcl-owned cycles on the creating thread.

    def assert_visible_status(self, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            if self.label.cget("text") == expected:
                break
            time.sleep(0.01)
        self.assertEqual(self.label.cget("text"), expected)
        self.assertTrue(self.label.winfo_ismapped())
        self.assertTrue(self.label.winfo_viewable())
        self.assertGreater(self.label.winfo_width(), 1)
        self.assertGreaterEqual(self.label.winfo_height(), self.label.winfo_reqheight())
        for container in (self.ui.panels[0], self.root):
            self.assertGreaterEqual(self.label.winfo_rootx(), container.winfo_rootx())
            self.assertGreaterEqual(self.label.winfo_rooty(), container.winfo_rooty())
            self.assertLessEqual(self.label.winfo_rootx() + self.label.winfo_width(),
                                 container.winfo_rootx() + container.winfo_width())
            self.assertLessEqual(self.label.winfo_rooty() + self.label.winfo_height(),
                                 container.winfo_rooty() + container.winfo_height())
        self.assertEqual(self.callbacks, [])
        print("VISIBLE %dx%d label=%dx%d text=%r" % (
            self.root.winfo_width(), self.root.winfo_height(),
            self.label.winfo_width(), self.label.winfo_height(), self.label.cget("text")))

    def to_connected(self):
        # Real controller handler used when the helper reports "ready".
        self.core.connection = "STARTING_SHELL"
        self.core._event({"type": "ready", "session": str(uuid.uuid4())})
        self.assertEqual(self.core.connection, "CONNECTED")
        self.ui.render()

    def test_connected_status_states_that_closing_the_app_keeps_the_shell(self):
        self.root.geometry("1360x850")
        self.assert_visible_status(WAITING)
        self.to_connected()
        self.assert_visible_status(CONNECTED)
        self.assertEqual(len(self.label.cget("text").split("\n")), 3)
        self.assertIn("App schließen beendet diese Shell nicht", self.label.cget("text"))

    def test_disconnected_status_names_both_causes_without_claiming_one(self):
        self.root.geometry("1360x850")
        self.to_connected()
        self.assert_visible_status(CONNECTED)
        # Real controller transition for terminal loss / shell end ("ended" or channel loss).
        self.core._disconnect()
        self.assertEqual(self.core.connection, "DISCONNECTED")
        self.ui.render()
        text = self.label.cget("text")
        self.assert_visible_status(DISCONNECTED)
        self.assertIn("Shell beendet", text)
        self.assertIn("Verbindung verloren", text)
        # The wording must offer both possibilities ("oder"), not assert a single cause.
        self.assertIn("oder", text)
        self.assertNotIn("\nUrsache: Shell beendet\n", text)
        self.assertNotIn("\nUrsache: Verbindung verloren\n", text)

    def test_status_transitions_waiting_connected_disconnected_waiting(self):
        self.root.geometry("960x560")
        self.assert_visible_status(WAITING)
        self.to_connected()
        self.assert_visible_status(CONNECTED)
        self.core._disconnect()
        self.ui.render()
        self.assert_visible_status(DISCONNECTED)
        # Reopening after loss returns to the waiting state (new endpoint awaited).
        self.core.restart_local()
        self.assertEqual(self.core.connection, "WAITING_FOR_TERMINAL")
        self.ui.render()
        self.assert_visible_status(WAITING)
        # STARTING_SHELL wording is unchanged by L06.
        self.core.connection = "STARTING_SHELL"
        self.ui.render()
        self.assert_visible_status(STARTING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
