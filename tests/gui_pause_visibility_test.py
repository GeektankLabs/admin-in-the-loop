"""Isolated Tk pause visibility test; no xterm or command execution.
Run from repo root: xvfb-run -a -s '-screen 0 1920x1080x24' python3 tests/gui_pause_visibility_test.py
"""
import gc
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_in_the_loop as a


class PauseVisibilityTests(unittest.TestCase):
    ACTIVE = "Aktiv · Freigaben nicht pausiert"
    PAUSED = "Pausiert · Neue Freigaben gesperrt\nBereits laufende Commands laufen weiter"

    def setUp(self):
        self.core = a.AppCore()
        self.root = tk.Tk()
        self.callbacks = []
        self.root.report_callback_exception = lambda *_: self.callbacks.append("tk_callback_failed")
        self.addCleanup(self.cleanup)
        self.ui = a.ReviewUI(self.root, self.core)
        self.label = self.ui.review_status_label
        self.root.update()

    def cleanup(self):
        self.core.close()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.ui = self.label = self.core = self.root = None
        gc.collect()  # Dispose Tcl-owned cycles on the creating thread.

    def assert_visible_status(self, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.root.update()
            # Read the actual label text, not the core state or its StringVar.
            if self.label.cget("text") == expected:
                break
            time.sleep(0.01)
        self.assertEqual(self.label.cget("text"), expected)
        self.assertTrue(self.label.winfo_ismapped())
        self.assertTrue(self.label.winfo_viewable())
        self.assertGreater(self.label.winfo_width(), 1)
        self.assertGreaterEqual(self.label.winfo_height(), self.label.winfo_reqheight())
        # The whole status label must be inside both the Review panel and window.
        for container in (self.ui.panels[1], self.root):
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

    def test_active_paused_active_is_rendered_by_existing_gui_tick(self):
        for width, height in ((960, 560), (1360, 850)):
            with self.subTest(size=(width, height)):
                self.root.geometry("%dx%d" % (width, height))
                self.assert_visible_status(self.ACTIVE)
                self.core.set_paused(True)
                self.assert_visible_status(self.PAUSED)
                self.core.set_paused(False)
                self.assert_visible_status(self.ACTIVE)

    def test_pause_keeps_transfer_and_error_status_visible(self):
        # Fixture only: no terminal/channel, bridge or command is started.
        self.core.review_state = "TRANSFER_UNCERTAIN"
        self.core.last_error = "transfer_uncertain"
        existing = "Übertragung ungewiss · keine Wiederholung | transfer_uncertain"
        self.root.geometry("960x560")
        self.assert_visible_status(self.ACTIVE + "\n" + existing)
        self.core.set_paused(True)
        self.assert_visible_status(self.PAUSED + "\n" + existing)
        self.core.set_paused(False)
        self.assert_visible_status(self.ACTIVE + "\n" + existing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
