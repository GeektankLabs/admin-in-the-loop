#!/usr/bin/env python3
"""Admin in the Loop: human-controlled MVP, Python >= 3.11.

Run: python3 admin_in_the_loop.py --self-test
GUI: python3 admin_in_the_loop.py  (or --gui)
Bridge-only: python3 admin_in_the_loop.py --bridge --state-dir DIR
Tkinter + native terminal + authenticated local IPC; optional transport-only bridge.
The helper is the only reader of the shell PTY; the Controller reads an IPC copy.
Both GUI and tests use AppCore. Tests use a clean temporary Bash, never sudo/SSH.
The GUI shell starts in the launching directory, without profiles or history.
This is a local prototype, not an isolation boundary or production security claim.
Synthetic password inputs exist transiently, but are never recorded as input.
Report output contains fixed test labels and metadata, never terminal content.
"""

from __future__ import annotations

import argparse
import errno
import gzip
import hashlib
import io
import json
import os
import platform
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


def _load_bridge_component(namespace=None):
    """Load the marked transport component alone, or expose it to controller/tests."""
    source = Path(__file__).read_text(encoding="utf-8")
    begin = "## BEGIN AITL EMBEDDED BRIDGE SOURCE\n"
    end = "\n## END AITL EMBEDDED BRIDGE SOURCE"
    start = source.index(begin) + len(begin)
    stop = source.index(end, start)
    if namespace is None:
        namespace = {"__name__": "_aitl_embedded_bridge", "__file__": str(Path(__file__).resolve())}
    exec(compile(source[start:stop], str(Path(__file__).resolve()), "exec"), namespace)
    return namespace


if len(sys.argv) > 1 and sys.argv[1] == "--bridge":
    try:
        _bridge_runtime = _load_bridge_component()
    except Exception:
        print("Stopped: bridge_runtime_failure (content omitted).", file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(_bridge_runtime["bridge_main"](sys.argv[2:]))
    except SystemExit:
        raise
    except _bridge_runtime["BridgeError"] as exc:
        print("Stopped: " + exc.code, file=sys.stderr)
        sys.exit(2)
    except Exception:
        print("Stopped: bridge_runtime_failure (content omitted).", file=sys.stderr)
        sys.exit(2)

if os.name == "posix":
    import fcntl
    import termios

PROMPT = b"AITL_P0$ "
MAX_COMMAND = 4096
MAX_LINES = 50


class ProbeError(Exception):
    """Fixed reason code only: never include commands, outputs, or secrets."""


class OverrideRequired(ProbeError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Stopped: invalid_cli_arguments (content omitted).\n")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProbeError(code)


def normalize_command(text: str, final_return: bool) -> bytes:
    if not isinstance(text, str) or type(final_return) is not bool:
        raise ProbeError("invalid_input_type")
    if len(text) > MAX_COMMAND:
        raise ProbeError("command_too_large")
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not text or not text.strip():
        raise ProbeError("empty_command")
    if final_return:
        text += "\n"
    for char in text:
        if char != "\n" and char != " " and (
            unicodedata.category(char).startswith("C")
            or unicodedata.category(char).startswith("Z")
        ):
            raise ProbeError("forbidden_character")
    try:
        payload = text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ProbeError("invalid_utf8") from None
    if len(payload) > MAX_COMMAND or len(text.splitlines()) > MAX_LINES:
        raise ProbeError("command_limit")
    return payload


@dataclass(frozen=True)
class OutputSlice:
    start: int
    end: int
    lost_prefix: bool
    data: bytes


class OutputRing:
    def __init__(self, capacity: int = 4 * 1024 * 1024):
        require(0 < capacity <= 4 * 1024 * 1024, "ring_capacity")
        self.capacity = capacity
        self.data = bytearray()
        self.end = 0
        self.dropped = 0

    @property
    def start(self) -> int:
        return self.end - len(self.data)

    def append(self, chunk: bytes) -> None:
        self.end += len(chunk)
        excess = max(0, len(self.data) + len(chunk) - self.capacity)
        self.dropped += excess
        if len(chunk) >= self.capacity:
            self.data[:] = chunk[-self.capacity:]
        else:
            if excess:
                del self.data[:excess]
            self.data.extend(chunk)

    def snapshot(self, start: int | None = None) -> OutputSlice:
        requested = self.start if start is None else start
        require(0 <= requested <= self.end, "invalid_output_offset")
        actual = max(requested, self.start)
        return OutputSlice(actual, self.end, requested < self.start,
                           bytes(self.data[actual - self.start:]))


@dataclass(frozen=True)
class ReviewSnapshot:
    id: str
    session_id: str
    generation: int
    payload: bytes
    final_return: bool


class ApprovalGate:
    """P0 single-threaded gate; UI/IPC event serialization is not implemented."""

    def __init__(self):
        self.states: dict[str, str] = {}
        self.snapshots: dict[str, ReviewSnapshot] = {}
        self.offsets: dict[str, int] = {}

    def stage(self, session: PtySession, text: str,
              final_return: bool = False) -> ReviewSnapshot:
        payload = normalize_command(text, final_return)
        snapshot = ReviewSnapshot(str(uuid.uuid4()), session.id,
                                  session.generation, payload, final_return)
        self.snapshots[snapshot.id] = snapshot
        self.states[snapshot.id] = "REVIEW"
        return snapshot

    def reject(self, snapshot: ReviewSnapshot) -> None:
        require(self.snapshots.get(snapshot.id) == snapshot, "unknown_snapshot")
        require(self.states.get(snapshot.id) == "REVIEW", "review_not_active")
        self.states[snapshot.id] = "REJECTED"

    def approve(self, session: PtySession, snapshot: ReviewSnapshot,
                *, override: bool = False, writer=None) -> int:
        require(self.snapshots.get(snapshot.id) == snapshot, "unknown_snapshot")
        require(self.states.get(snapshot.id) == "REVIEW", "approval_already_consumed")
        require(snapshot.session_id == session.id and
                snapshot.generation == session.generation, "stale_session")
        require(not session.closed and session.proc.poll() is None, "shell_not_alive")
        session.pump()
        if not session.prompt_hint() and not override:
            raise OverrideRequired("prompt_unknown")
        # Consume BEFORE the first write. Failure must not make this reusable.
        self.states[snapshot.id] = "TRANSFERRING"
        self.offsets[snapshot.id] = session.output.end
        try:
            count = session.write_transient(snapshot.payload, writer=writer)
        except Exception:
            self.states[snapshot.id] = "TRANSFER_UNCERTAIN"
            raise ProbeError("transfer_uncertain") from None
        self.states[snapshot.id] = "TRANSFERRED"
        return count


class PtySession:
    """One owner, no input history; bytes are captured only from PTY reads."""

    def __init__(self, directory: str, *, capacity: int = 4 * 1024 * 1024):
        require(os.name == "posix", "unix_required")
        bash = shutil.which("bash")
        require(bool(bash), "bash_required")
        self.id = str(uuid.uuid4())
        self.generation = 0
        self.closed = False
        self.eof = False
        self.output = OutputRing(capacity)
        self.master, slave = os.openpty()
        termios.tcsetwinsize(slave, (24, 80))
        # Explicit allowlist: no inherited bridge tokens, SSH agent or Python path.
        # Tests use no startup files and disable shell history.
        child_env = {
            "PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "LC_ALL": "C",
            "PS1": PROMPT.decode("ascii"), "PS2": "AITL_MORE> ",
            "HISTFILE": "/dev/null", "HISTSIZE": "0", "HISTFILESIZE": "0",
        }
        try:
            # Fresh helper process, never preexec_fn or fork from an active GUI.
            self.proc = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()),
                 "--_shell-child", str(slave), str(bash)],
                cwd=directory, env=child_env, close_fds=True, pass_fds=(slave,),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)

    def __enter__(self):
        try:
            self.wait_for(PROMPT)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_):
        self.close()

    def pump(self, timeout: float = 0, budget: int = 256 * 1024) -> int:
        if self.closed or self.eof:
            return 0
        total = 0
        while total < budget:
            readable, _, _ = select.select([self.master], [], [], timeout if total == 0 else 0)
            if not readable:
                break
            try:
                chunk = os.read(self.master, min(65536, budget - total))
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:  # Linux EOF; BSD/macOS may return b''.
                    self.eof = True
                    break
                raise ProbeError("pty_read_failure") from None
            if not chunk:
                self.eof = True
                break
            self.output.append(chunk)
            total += len(chunk)
        return total

    def wait_until(self, predicate, timeout: float = 4) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(min(0.05, max(0, deadline - time.monotonic())))
            if predicate():
                return
        raise ProbeError("wait_timeout")

    def wait_for(self, needle: bytes, *, start: int | None = None,
                 timeout: float = 4) -> OutputSlice:
        self.wait_until(lambda: needle in self.output.snapshot(start).data, timeout)
        return self.output.snapshot(start)

    def prompt_hint(self) -> bool:
        # Deliberately soft. A program can emit this exact visual pattern.
        if self.closed or self.eof or self.proc.poll() is not None:
            return False
        # Readline also disables kernel ECHO at a normal shell prompt.
        # ECHO cannot distinguish this from a password request.
        tail = bytes(self.output.data[-512:])
        # Small prompt hint only, NOT a full terminal sanitizer or screen model.
        tail = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", tail)
        return bool(re.search(rb"(?:^|[\r\n])[^\r\n\x1b]{0,200}[$#%] $", tail))

    def write_transient(self, payload: bytes, *, writer=None,
                        timeout: float = 2) -> int:
        # payload is referenced for this write only, never added to an input log.
        require(not self.closed and self.proc.poll() is None, "shell_not_alive")
        send = os.write if writer is None else writer
        view = memoryview(payload)
        count = 0
        deadline = time.monotonic() + timeout
        while count < len(view):
            if time.monotonic() >= deadline:
                raise ProbeError("write_timeout")
            try:
                written = send(self.master, view[count:])
            except BlockingIOError:
                self.pump()
                select.select([], [self.master], [], 0.02)
                continue
            except InterruptedError:
                continue
            except OSError:
                raise ProbeError("pty_write_failure") from None
            if not isinstance(written, int) or written <= 0 or written > len(view) - count:
                raise ProbeError("invalid_write_count")
            count += written
        return count

    def resize(self, rows: int, columns: int) -> None:
        require(1 <= rows <= 1000 and 1 <= columns <= 1000, "invalid_geometry")
        termios.tcsetwinsize(self.master, (rows, columns))

    def close(self) -> None:
        if self.closed:
            return
        self.generation += 1
        self.pump()
        if self.proc.poll() is None:
            # Current foreground group only: do not signal historical group IDs
            # which may have exited and subsequently been reused by the OS.
            targets = {self.proc.pid}
            try:
                current = os.tcgetpgrp(self.master)
                if current > 0:
                    targets.add(current)
            except OSError:
                pass
            targets.discard(os.getpgrp())
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                for group in targets:
                    try:
                        os.killpg(group, sig)
                    except ProcessLookupError:
                        pass
                try:
                    self.proc.wait(timeout=0.4)
                    break
                except subprocess.TimeoutExpired:
                    pass
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=1)
        else:
            self.proc.wait(timeout=1)
        os.close(self.master)
        self.closed = True


def shell_child(fd: int, bash: str) -> None:
    """Internal trusted role. Attach controlling tty, then replace with Bash."""
    os.setsid()
    fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    for destination in (0, 1, 2):
        os.dup2(fd, destination)
    if fd > 2:
        os.close(fd)
    os.execve(bash, [bash, "--noprofile", "--norc", "-i"], dict(os.environ))


def expect_error(fn, code: str | None = None) -> None:
    try:
        fn()
    except ProbeError as exc:
        if code:
            require(str(exc) == code, "unexpected_error_code")
        return
    raise ProbeError("expected_rejection_missing")


def marker_command(marker: str) -> str:
    # Fixed local test identifiers, never external text.
    require(bool(re.fullmatch(r"[A-Z0-9_]+", marker)), "invalid_test_marker")
    return "printf '\\n" + marker + "\\n'"


def test_terminal_identity(directory):
    with PtySession(directory) as s:
        start = s.output.end
        s.write_transient(b"test -t 0 && test -t 1 && test -t 2 && printf '\\nTTY_OK\\n'\n")
        s.wait_for(b"\r\nTTY_OK\r\n", start=start)
        require(os.tcgetpgrp(s.master) == s.proc.pid, "shell_not_foreground")


def test_pending_rejected_and_approved(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, ": > pending-marker; " + marker_command("APPROVED"), True)
        s.pump(0.1)
        require(not Path(directory, "pending-marker").exists(), "pending_executed")
        rejected = gate.stage(s, ": > rejected-marker", True)
        gate.reject(rejected)
        expect_error(lambda: gate.approve(s, rejected), "approval_already_consumed")
        require(not Path(directory, "rejected-marker").exists(), "rejected_executed")
        count = gate.approve(s, item)
        s.wait_for(b"\r\nAPPROVED\r\n", start=gate.offsets[item.id])
        require(Path(directory, "pending-marker").exists(), "approval_not_effective")
        require(count == len(item.payload), "transfer_count")


def test_duplicate(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, "printf x >> once-marker; " + marker_command("ONCE"), True)
        gate.approve(s, item)
        s.wait_for(b"\r\nONCE\r\n", start=gate.offsets[item.id])
        expect_error(lambda: gate.approve(s, item), "approval_already_consumed")
        require(Path(directory, "once-marker").read_bytes() == b"x", "duplicate_transfer")


def test_frozen_review(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        source = {"text": marker_command("FROZEN")}
        item = gate.stage(s, source["text"], True)
        source["text"] = ": > forbidden-mutated-marker"
        gate.stage(s, source["text"], True)  # New inbox item cannot replace this one.
        gate.approve(s, item)
        s.wait_for(b"\r\nFROZEN\r\n", start=gate.offsets[item.id])
        require(not Path(directory, "forbidden-mutated-marker").exists(), "snapshot_changed")


def test_stale_session(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, ": > stale-marker", True)
        s.generation += 1
        expect_error(lambda: gate.approve(s, item), "stale_session")
        # Override must not bypass session identity.
        expect_error(lambda: gate.approve(s, item, override=True), "stale_session")
        require(not Path(directory, "stale-marker").exists(), "stale_executed")


def test_normalization(_):
    require(normalize_command("a\r\nb\r\n\r\n", True) == b"a\nb\n", "crlf_on")
    require(normalize_command("a\r\nb\r\n\r\n", False) == b"a\nb", "crlf_off")
    require(normalize_command("a\rb", True) == b"a\nb\n", "cr_on")
    require(normalize_command("printf '€'", True).endswith(b"\xe2\x82\xac'\n"), "utf8_preservation")
    for char in ("\x00", "\x1b", "\t", "\x7f", "\x85", "\u202e", "\u200b", "\u2028", "\ud800"):
        expect_error(lambda char=char: normalize_command("a" + char, True))
    expect_error(lambda: normalize_command("x" * 4097, False))
    expect_error(lambda: normalize_command("x\n" * 51, True))


def test_no_final_return(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, ": > enter-marker; " + marker_command("ENTER"), False)
        gate.approve(s, item)
        s.pump(0.15)
        require(not Path(directory, "enter-marker").exists(), "hidden_enter")
        s.write_transient(b"\n")  # Explicit synthetic manual Enter.
        s.wait_for(b"\r\nENTER\r\n", start=gate.offsets[item.id])
        require(Path(directory, "enter-marker").exists(), "manual_enter_missing")


def test_multiline_internal_return(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, ": > internal-marker\n: > final-marker", False)
        gate.approve(s, item)
        s.wait_until(lambda: Path(directory, "internal-marker").exists())
        require(not Path(directory, "final-marker").exists(), "unexpected_final_return")
        s.write_transient(b"\n")
        s.wait_until(lambda: Path(directory, "final-marker").exists())


def test_password_echo_off(directory):
    with PtySession(directory) as s:
        start = s.output.end
        s.write_transient(b"IFS= read -r -s -p 'AITL_PASSWORD: ' pw; printf '\\nPW_LEN=%s\\n' \"${#pw}\"; unset pw\n")
        s.wait_until(lambda: s.output.snapshot(start).data.endswith(b"AITL_PASSWORD: "))
        require(not termios.tcgetattr(s.master)[3] & termios.ECHO, "password_echo_enabled")
        require(not s.prompt_hint(), "password_prompt_claimed_ready")
        # Synthetic, never real. Kept only in this test's transient local memory.
        password = ("synthetic_" + uuid.uuid4().hex).encode("ascii")
        s.write_transient(password + b"\n")
        s.wait_for(b"\r\nPW_LEN=" + str(len(password)).encode() + b"\r\n", start=start)
        require(password not in s.output.snapshot().data, "password_in_output")
        s.wait_until(lambda: s.output.snapshot().data.endswith(PROMPT))


def test_echo_on_positive_control(directory):
    with PtySession(directory) as s:
        start = s.output.end
        s.write_transient(b"IFS= read -r -p 'AITL_ECHO: ' pw; printf '\\nECHO_DONE\\n'; unset pw\n")
        s.wait_until(lambda: s.output.snapshot(start).data.endswith(b"AITL_ECHO: "))
        password = ("synthetic_" + uuid.uuid4().hex).encode("ascii")
        s.write_transient(password + b"\n")
        s.wait_for(b"\r\nECHO_DONE\r\n", start=start)
        require(password in s.output.snapshot(start).data, "positive_echo_control_failed")


def test_prompt_override_and_false_positive(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        start = s.output.end
        s.write_transient(b"IFS= read -r -p 'AITL_WAIT: ' answer; printf '\\nWAIT_DONE\\n'; unset answer\n")
        s.wait_until(lambda: s.output.snapshot(start).data.endswith(b"AITL_WAIT: "))
        require(not s.prompt_hint(), "unexpected_hint")
        item = gate.stage(s, "test-input", True)
        expect_error(lambda: gate.approve(s, item), "prompt_unknown")
        require(gate.states[item.id] == "REVIEW", "override_consumed_early")
        gate.approve(s, item, override=True)
        s.wait_for(b"\r\nWAIT_DONE\r\n", start=start)
        s.wait_until(lambda: s.output.snapshot().data.endswith(PROMPT))
        start = s.output.end
        s.write_transient(b"IFS= read -r -p 'FAKE$ ' answer; printf '\\nFAKE_DONE\\n'; unset answer\n")
        s.wait_until(lambda: s.output.snapshot(start).data.endswith(b"FAKE$ "))
        require(s.prompt_hint(), "false_positive_demonstration_missing")
        # Prove that a program prompt looks ready, without claiming shell safety.
        fake = gate.stage(s, ": > must-not-execute", True)
        gate.approve(s, fake)
        s.wait_for(b"\r\nFAKE_DONE\r\n", start=start)
        require(not Path(directory, "must-not-execute").exists(), "read_did_not_consume_text")


def test_resize_and_signal(directory):
    with PtySession(directory) as s:
        s.write_transient(b"trap 'printf \"\\nRESIZE_SIGNAL\\n\"' WINCH\n")
        s.wait_until(lambda: s.output.snapshot().data.endswith(PROMPT) and b"trap '" in s.output.data)
        start = s.output.end
        s.resize(37, 111)
        s.wait_for(b"\r\nRESIZE_SIGNAL\r\n", start=start)
        s.write_transient(b"stty size; printf '\\nSIZE_DONE\\n'\n")
        data = s.wait_for(b"\r\nSIZE_DONE\r\n", start=start).data
        require(bool(re.search(rb"(?:^|[\r\n])37 111\r\n", data)), "geometry_not_updated")


def test_ctrl_c(directory):
    with PtySession(directory) as s:
        start = s.output.end
        # A foreground process group can appear before exec/signal setup finishes.
        s.write_transient(b"bash -c 'printf \"\\nP0_FOREGROUND_READY\\n\"; exec sleep 30'\n")
        s.wait_for(b"\r\nP0_FOREGROUND_READY\r\n", start=start)
        require(os.tcgetpgrp(s.master) not in (0, s.proc.pid), "foreground_job_not_started")
        s.write_transient(b"\x03")
        s.wait_for(PROMPT, start=start)
        require(s.proc.poll() is None, "shell_killed_by_ctrl_c")
        s.write_transient((marker_command("AFTER_INT") + "\n").encode())
        s.wait_for(b"\r\nAFTER_INT\r\n", start=start)


def test_output_offset_and_snapshot(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        s.write_transient((marker_command("BEFORE") + "\n").encode())
        s.wait_for(b"\r\nBEFORE\r\n")
        s.wait_until(lambda: s.output.snapshot().data.endswith(PROMPT))
        item = gate.stage(s, marker_command("AFTER"), True)
        gate.approve(s, item)
        frozen = s.wait_for(b"\r\nAFTER\r\n", start=gate.offsets[item.id])
        require(b"\r\nBEFORE\r\n" not in frozen.data, "offset_includes_old_observation")
        s.write_transient(b"(sleep 0.1; printf '\\nBACKGROUND\\n') &\n")
        s.wait_for(b"\r\nBACKGROUND\r\n", start=gate.offsets[item.id])
        s.write_transient((marker_command("MANUAL_LATER") + "\n").encode())
        later = s.wait_for(b"\r\nMANUAL_LATER\r\n", start=gate.offsets[item.id])
        require(b"\r\nBACKGROUND\r\n" in later.data, "background_missing")
        require(b"MANUAL_LATER" not in frozen.data, "output_snapshot_changed")


def test_ring_overflow(directory):
    with PtySession(directory, capacity=2048) as s:
        start = s.output.end
        s.write_transient(b"for ((i=0;i<4000;i++)); do printf x; done; printf '\\nOVERFLOW_DONE\\n'\n")
        data = s.wait_for(b"\r\nOVERFLOW_DONE\r\n", start=start)
        require(len(s.output.data) <= 2048, "ring_unbounded")
        require(data.lost_prefix and s.output.dropped > 0, "overflow_not_signaled")


def test_partial_write_success(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, marker_command("PARTIAL_OK"), True)
        count = gate.approve(s, item, writer=lambda fd, data: os.write(fd, data[:3]))
        s.wait_for(b"\r\nPARTIAL_OK\r\n", start=gate.offsets[item.id])
        require(count == len(item.payload), "partial_count")


def test_partial_write_failure(directory):
    with PtySession(directory) as s:
        gate = ApprovalGate()
        item = gate.stage(s, marker_command("NEVER_COMPLETE"), True)
        calls = 0

        def fail_after_prefix(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return os.write(fd, data[:5])
            raise OSError(errno.EIO, "synthetic")

        expect_error(lambda: gate.approve(s, item, writer=fail_after_prefix), "transfer_uncertain")
        require(gate.states[item.id] == "TRANSFER_UNCERTAIN", "uncertainty_not_recorded")
        expect_error(lambda: gate.approve(s, item, override=True), "approval_already_consumed")
        require(calls == 2, "automatic_retry")


def test_clean_environment(directory):
    name = "AITL_P0_PARENT_SECRET"
    previous = os.environ.get(name)
    os.environ[name] = "synthetic_parent_only"
    try:
        with PtySession(directory) as s:
            start = s.output.end
            s.write_transient(b"if [[ -z ${AITL_P0_PARENT_SECRET+x} && -z ${SSH_AUTH_SOCK+x} ]]; then printf '\\nENV_CLEAN\\n'; fi\n")
            s.wait_for(b"\r\nENV_CLEAN\r\n", start=start)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def test_fragmented_utf8(directory):
    with PtySession(directory) as s:
        start = s.output.end
        s.write_transient(b"printf '\\342'; sleep 0.05; printf '\\202\\254\\nUTF8_DONE\\n'\n")
        data = s.wait_for(b"\r\nUTF8_DONE\r\n", start=start)
        require(b"\xe2\x82\xac\r\n" in data.data, "fragmented_utf8_corrupted")


def test_eof_and_cleanup(directory):
    s = PtySession(directory)
    try:
        s.wait_for(PROMPT)
        s.write_transient(b"exit 0\n")
        s.wait_until(lambda: s.eof and s.proc.poll() is not None)
        require(s.proc.returncode == 0, "exit_status")
    finally:
        s.close()
    require(s.closed and s.proc.poll() is not None, "child_not_reaped")
    expect_error(lambda: s.write_transient(b"x"), "shell_not_alive")
    try:
        os.fstat(s.master)
    except OSError as exc:
        require(exc.errno == errno.EBADF, "unexpected_fd_error")
    else:
        raise ProbeError("master_not_closed")
    s.close()  # Idempotent local shutdown.


def test_shutdown_foreground(directory):
    s = PtySession(directory)
    try:
        s.wait_for(PROMPT)
        s.write_transient(b"sleep 30\n")
        s.wait_until(lambda: os.tcgetpgrp(s.master) not in (0, s.proc.pid))
        started = time.monotonic()
        s.close()
        require(time.monotonic() - started < 3, "shutdown_unbounded")
        require(s.proc.poll() is not None, "shell_not_reaped")
    finally:
        s.close()


TESTS = [
    ("controlling_tty_and_job_control", test_terminal_identity),
    ("pending_reject_approve", test_pending_rejected_and_approved),
    ("single_use_approval", test_duplicate),
    ("immutable_review", test_frozen_review),
    ("stale_session_blocked", test_stale_session),
    ("return_unicode_and_limits", test_normalization),
    ("no_hidden_final_enter", test_no_final_return),
    ("internal_newlines_execute", test_multiline_internal_return),
    ("password_echo_off", test_password_echo_off),
    ("echo_on_positive_control", test_echo_on_positive_control),
    ("prompt_override_and_false_positive", test_prompt_override_and_false_positive),
    ("resize_and_sigwinch", test_resize_and_signal),
    ("ctrl_c_foreground_interrupt", test_ctrl_c),
    ("output_offset_background_and_frozen_selection", test_output_offset_and_snapshot),
    ("bounded_output_overflow", test_ring_overflow),
    ("short_write_success", test_partial_write_success),
    ("short_write_failure_no_retry", test_partial_write_failure),
    ("credential_environment_not_inherited", test_clean_environment),
    ("fragmented_utf8_capture", test_fragmented_utf8),
    ("eof_fd_close_and_reaping", test_eof_and_cleanup),
    ("bounded_shutdown_with_foreground_job", test_shutdown_foreground),
]


def self_test() -> int:
    started = time.monotonic()
    results = []
    for label, test in TESTS:
        tick = time.monotonic()
        result = {"test": label}
        # Directory destruction leaves no command-marker artifacts behind.
        with tempfile.TemporaryDirectory(prefix="aitl-p0-") as directory:
            try:
                test(directory)
                result["status"] = "PASS"
            except ProbeError as exc:
                result.update(status="FAIL", reason=str(exc))
            except Exception as exc:
                # No exception text/tracebacks: may contain terminal or secret data.
                result.update(status="FAIL", reason=type(exc).__name__)
        result["seconds"] = round(time.monotonic() - tick, 3)
        results.append(result)
    failed = sum(row["status"] != "PASS" for row in results)
    report = {
        "project": "Admin in the Loop", "stage": "P0 headless PTY probe",
        "python": platform.python_version(), "platform": platform.system(),
        "machine": platform.machine(), "tests": results,
        "passed": len(results) - failed, "failed": failed,
        "seconds": round(time.monotonic() - started, 3),
        "not_tested": ["GUI", "native-terminal relay", "IPC authentication",
                       "network/MCP", "TLS pairing", "real SSH/sudo",
                       "full terminal sanitizer", "simultaneous typing",
                       "macOS" if platform.system() != "Darwin" else "Linux"],
    }
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 1 if failed else 0

# This section is appended to the single distributable file during development.
import base64
import hashlib
import hmac
import secrets
import shlex
import socket
import stat
import struct
import tty
import unittest
from collections import deque

FRAME_LIMIT = 128 * 1024
QUEUE_LIMIT = 512 * 1024
OUTPUT_LIMIT = 64 * 1024


def packet(value: dict) -> bytes:
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                     allow_nan=False).encode("ascii")
    require(0 < len(raw) <= FRAME_LIMIT, "frame_limit")
    return struct.pack("!I", len(raw)) + raw


def decode_packet(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ProbeError("json_constant")))
    except ProbeError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError("invalid_json") from None
    require(type(result) is dict, "json_object_required")
    return result


def fields(value, names):
    require(type(value) is dict and set(value) == set(names), "invalid_schema")


def bounded_string(value, limit=4096):
    require(type(value) is str and len(value) <= limit, "invalid_string")
    return value


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        require(bool(chunk), "ipc_eof")
        data.extend(chunk)
    return bytes(data)


def receive_blocking(sock):
    size = struct.unpack("!I", read_exact(sock, 4))[0]
    require(0 < size <= FRAME_LIMIT, "frame_limit")
    return decode_packet(read_exact(sock, size))


def proof(key, role, server_nonce, client_nonce):
    message = ("AITL-LOCAL-1|" + role + "|" + server_nonce + "|" + client_nonce).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class Channel:
    """Bounded, nonblocking, single-owner local stream; no pickle/eval."""
    def __init__(self, sock):
        self.sock = sock
        sock.setblocking(False)
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.partial_since = None
        self.closed = False
        self.read_eof = False

    def send(self, value):
        raw = packet(value)
        require(len(self.outgoing) + len(raw) <= QUEUE_LIMIT, "ipc_backpressure")
        self.outgoing.extend(raw)

    def flush(self):
        if self.outgoing:
            try:
                count = self.sock.send(self.outgoing)
            except BlockingIOError:
                return
            require(count > 0, "ipc_eof")
            del self.outgoing[:count]

    def receive(self):
        require(not self.read_eof, "ipc_eof")
        messages = []
        # A fixed budget prevents malicious peers starving terminal/UI events.
        for _ in range(8):
            try:
                chunk = self.sock.recv(32768)
            except BlockingIOError:
                break
            if not chunk:
                self.read_eof = True
                require(bool(messages) and not self.incoming, "ipc_eof")
                break
            self.incoming.extend(chunk)
            while len(self.incoming) >= 4:
                size = struct.unpack("!I", self.incoming[:4])[0]
                require(0 < size <= FRAME_LIMIT, "frame_limit")
                if len(self.incoming) < size + 4:
                    break
                messages.append(decode_packet(bytes(self.incoming[4:4 + size])))
                del self.incoming[:4 + size]
            require(len(self.incoming) <= FRAME_LIMIT + 4, "frame_limit")
        if self.incoming:
            self.partial_since = self.partial_since or time.monotonic()
            require(time.monotonic() - self.partial_since < 5, "frame_timeout")
        else:
            self.partial_since = None
        return messages

    def close(self):
        if not self.closed:
            self.sock.close()
            self.closed = True


class LocalEndpoint:
    """Controller-owned AF_UNIX endpoint. Same UID remains trusted."""
    def __init__(self, workdir):
        require(Path(workdir).is_dir(), "invalid_workdir")
        self.workdir = str(Path(workdir).resolve())
        self.directory = Path(tempfile.mkdtemp(prefix="aitl-", dir="/tmp"))
        os.chmod(self.directory, 0o700)
        self.key = secrets.token_bytes(32)
        self.capfile = self.directory / "capability"
        fd = os.open(self.capfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(self.key)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.directory / "controller.sock"))
            os.chmod(self.directory / "controller.sock", 0o600)
            self.listener.listen(1)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            shutil.rmtree(self.directory, ignore_errors=True)
            raise
        self.pending = None
        self.pending_since = None
        self.server_nonce = None
        self.bound = False

    def accept(self):
        if self.bound:
            return None
        if self.pending is None:
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return None
            self.pending = Channel(sock)
            self.pending_since = time.monotonic()
            self.server_nonce = secrets.token_hex(32)
            self.pending.send({"type": "challenge", "nonce": self.server_nonce, "version": 1})
        try:
            require(time.monotonic() - self.pending_since < 5, "auth_timeout")
            self.pending.flush()
            messages = self.pending.receive()
            if not messages:
                return None
            require(len(messages) == 1, "auth_extra_frames")
            auth = messages[0]
            fields(auth, {"type", "nonce", "proof"})
            require(auth["type"] == "authenticate", "auth_type")
            nonce = bounded_string(auth["nonce"], 64)
            require(bool(re.fullmatch("[0-9a-f]{64}", nonce)), "auth_nonce")
            require(hmac.compare_digest(bounded_string(auth["proof"], 64),
                    proof(self.key, "helper", self.server_nonce, nonce)), "auth_failed")
            channel = self.pending
            channel.send({"type": "authenticated", "proof": proof(self.key, "controller", self.server_nonce, nonce),
                          "workdir": self.workdir})
            channel.flush()
            self.pending = None
            self.bound = True
            self.capfile.unlink()
            self.key = b""  # Python cannot guarantee secure erasure.
            self.listener.close()  # No second connection or reconnect.
            return channel
        except (ProbeError, OSError):
            self.pending.close()
            self.pending = None
            return None

    def close(self):
        if self.pending:
            self.pending.close()
            self.pending = None
        self.listener.close()
        shutil.rmtree(self.directory, ignore_errors=True)
        self.key = b""


def helper_connect(directory):
    directory = Path(directory)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_session_directory")
    fd = os.open(directory / "capability", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "unsafe_capability")
        key = source.read(33)
    require(len(key) == 32, "capability_length")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(str(directory / "controller.sock"))
        challenge = receive_blocking(sock)
        fields(challenge, {"type", "nonce", "version"})
        require(challenge["type"] == "challenge" and type(challenge["version"]) is int
                and challenge["version"] == 1, "protocol_version")
        nonce = bounded_string(challenge["nonce"], 64)
        require(bool(re.fullmatch("[0-9a-f]{64}", nonce)), "auth_nonce")
        mine = secrets.token_hex(32)
        sock.sendall(packet({"type": "authenticate", "nonce": mine,
                             "proof": proof(key, "helper", nonce, mine)}))
        reply = receive_blocking(sock)
        fields(reply, {"type", "proof", "workdir"})
        require(reply["type"] == "authenticated" and hmac.compare_digest(
            bounded_string(reply["proof"], 64), proof(key, "controller", nonce, mine)), "auth_failed")
        workdir = bounded_string(reply["workdir"], 8192)
        require(Path(workdir).is_dir(), "invalid_workdir")
        return Channel(sock), workdir
    except BaseException:
        sock.close()
        raise


def helper_transfer(session, request, consumed, manual_dirty):
    fields(request, {"type", "id", "session", "text", "return", "override"})
    require(request["type"] == "transfer", "request_type")
    identifier = bounded_string(request["id"], 64)
    require(bool(re.fullmatch("[0-9a-f]{32}", identifier)), "review_id")
    require(request["session"] == session.id, "stale_session")
    require(type(request["override"]) is bool, "override_type")
    payload = normalize_command(request["text"], request["return"])
    require(identifier not in consumed, "approval_already_consumed")
    require(len(consumed) < 1000, "session_transfer_limit")
    session.pump()
    if (manual_dirty or not session.prompt_hint()) and not request["override"]:
        return {"type": "result", "id": identifier, "state": "OVERRIDE_REQUIRED", "offset": session.output.end}
    consumed.add(identifier)  # Before any bytes, including a failing write.
    offset = session.output.end
    try:
        session.write_transient(payload)
    except Exception:
        return {"type": "result", "id": identifier, "state": "TRANSFER_UNCERTAIN", "offset": offset}
    return {"type": "result", "id": identifier, "state": "TRANSFERRED", "offset": offset}


def run_terminal_helper(directory):
    require(os.isatty(0) and os.isatty(1), "terminal_required")
    channel, workdir = helper_connect(directory)
    original = termios.tcgetattr(0)
    consumed = set()
    manual_dirty = False
    terminal_queue = bytearray()
    sent_offset = 0
    old_output_flags = fcntl.fcntl(1, fcntl.F_GETFL)
    resizing = True
    terminate = False
    previous_handlers = {}

    def handle_signal(signum, _frame):
        nonlocal resizing, terminate
        if signum == signal.SIGWINCH:
            resizing = True
        else:
            terminate = True

    try:
        for sig in (signal.SIGWINCH, signal.SIGHUP, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, handle_signal)
        tty.setraw(0)
        fcntl.fcntl(1, fcntl.F_SETFL, old_output_flags | os.O_NONBLOCK)
        with PtySession(workdir) as session:
            channel.send({"type": "ready", "session": session.id})
            last_geometry = None
            while not terminate:
                if resizing:
                    resizing = False
                    rows, cols = termios.tcgetwinsize(0)
                    geometry = (max(1, rows), max(1, cols))
                    if geometry != last_geometry:
                        session.resize(*geometry)
                        last_geometry = geometry
                # Manual input waiting before a transfer is handled first.
                readable, _, _ = select.select([0, session.master], [], [], 0.01)
                if 0 in readable:
                    data = os.read(0, 4096)
                    if not data:
                        break
                    session.write_transient(data)
                    manual_dirty = True
                session.pump()
                if session.output.end > sent_offset:
                    output = session.output.snapshot(sent_offset)
                    require(not output.lost_prefix, "terminal_output_loss")
                    terminal_queue.extend(output.data)
                    require(len(terminal_queue) <= QUEUE_LIMIT, "terminal_backpressure")
                    if channel:
                        # Copy only observed output; never copy manual input.
                        try:
                            for begin in range(0, len(output.data), 32768):
                                channel.send({"type": "output", "start": sent_offset + begin,
                                              "data": base64.b64encode(output.data[begin:begin + 32768]).decode("ascii")})
                        except ProbeError:
                            # Drop controller copy, not native terminal output.
                            channel.close()
                            channel = None
                    sent_offset = session.output.end
                    if session.prompt_hint():
                        manual_dirty = False
                if terminal_queue:
                    try:
                        count = os.write(1, terminal_queue)
                        del terminal_queue[:count]
                    except BlockingIOError:
                        pass
                if channel:
                    try:
                        channel.flush()
                        for request in channel.receive():
                            if request.get("type") == "detach":
                                fields(request, {"type"})
                                channel.close()
                                channel = None
                                break
                            result = helper_transfer(session, request, consumed, manual_dirty)
                            channel.send(result)
                            if result["state"] == "TRANSFERRED":
                                manual_dirty = True
                        if channel:
                            channel.flush()
                    except (ProbeError, OSError):
                        # Disable all AI transfers on protocol loss. Manual shell survives.
                        channel.close()
                        channel = None
                if channel is None:
                    # No persistent capture after Controller disconnect.
                    session.output.data.clear()
                if session.eof:
                    if channel:
                        channel.send({"type": "ended"})
                        channel.flush()
                    break
    finally:
        if channel:
            channel.close()
        fcntl.fcntl(1, fcntl.F_SETFL, old_output_flags)
        termios.tcsetattr(0, termios.TCSANOW, original)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def sanitized_output(raw):
    """Bounded transcript, not a screen emulator. Strip CSI/OSC/DCS; mark controls."""
    require(len(raw) <= OUTPUT_LIMIT, "output_selection_limit")
    text = raw.decode("utf-8", errors="surrogateescape")
    result = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\x1b":
            start = i
            i += 1
            if i < len(text) and text[i] == "[":
                i += 1
                while i < len(text) and i - start < 4096 and not ("@" <= text[i] <= "~"):
                    i += 1
                if i < len(text) and "@" <= text[i] <= "~":
                    i += 1
                    continue
            elif i < len(text) and text[i] in "]P^_":
                i += 1
                while i < len(text) and i - start < 4096:
                    if text[i] == "\x07":
                        i += 1
                        break
                    if text[i:i + 2] == "\x1b\\":
                        i += 2
                        break
                    i += 1
                else:
                    result.append("[unterbrochene Terminalsequenz]")
                continue
            elif i < len(text) and "0" <= text[i] <= "~":
                i += 1
                continue
            result.append("[unterbrochene Terminalsequenz]")
            continue
        if char == "\r" and text[i:i + 2] == "\r\n":
            result.append("\n")
            i += 1
        elif char == "\n" or char == " ":
            result.append(char)
        elif unicodedata.category(char).startswith(("C", "Z")):
            result.append("[U+%04X]" % ord(char))
        else:
            result.append(char)
        i += 1
    value = "".join(result)
    require(len(value.encode("utf-8")) <= OUTPUT_LIMIT, "output_selection_limit")
    return value


class TranscriptSanitizer:
    """Streaming projection with O(1) escape state and an incremental UTF-8 decoder.

    Never buffers OSC/DCS contents. After 4096 characters it marks the discard
    and continues dropping until the terminator; a newline cannot expose the
    middle of an invisible terminal sequence in a last-N-lines selection.
    """
    def __init__(self):
        import codecs
        self.decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self.state, self.count, self.warned = "TEXT", 0, False
        self.cr = False

    def feed(self, raw):
        result = []
        for char in self.decoder.decode(raw):
            if self.state != "TEXT":
                self.count = min(4097, self.count + 1)
                if self.count > 4096 and not self.warned:
                    result.append("[Terminalsequenz verworfen]")
                    self.warned = True
                if self.state == "ESC":
                    if char == "[":
                        self.state = "CSI"
                    elif char in "]P^_X":
                        self.state = "STRING"
                    elif " " <= char <= "/":
                        self.state = "ESC_INTERMEDIATE"
                    else:
                        self.state = "TEXT"
                elif self.state in {"CSI", "ESC_INTERMEDIATE"}:
                    if "@" <= char <= "~" if self.state == "CSI" else "0" <= char <= "~":
                        self.state = "TEXT"
                elif self.state == "STRING":
                    if char in {"\x07", "\x9c"}:
                        self.state = "TEXT"
                    elif char == "\x1b":
                        self.state = "STRING_ESC"
                elif self.state == "STRING_ESC":
                    self.state = "TEXT" if char == "\\" else "STRING_ESC" if char == "\x1b" else "STRING"
                continue
            if self.cr:
                self.cr = False
                if char == "\n":
                    result.append("\n")
                    continue
                result.append("[U+000D]")
            if char in {"\x1b", "\x9b", "\x9d", "\x90"}:
                self.state = {"\x1b": "ESC", "\x9b": "CSI", "\x9d": "STRING", "\x90": "STRING"}[char]
                self.count, self.warned = 0, False
            elif char == "\r":
                self.cr = True
            elif char in {"\n", " "}:
                result.append(char)
            elif unicodedata.category(char).startswith(("C", "Z")):
                result.append("[U+%04X]" % ord(char))
            else:
                result.append(char)
        return "".join(result).encode("utf-8")

    def pending_marker(self):
        if self.state != "TEXT":
            return b"[unterbrochene Terminalsequenz]"
        if self.decoder.getstate()[0]:
            return b"[unvollstaendiges UTF-8]"
        return b"[U+000D]" if self.cr else b""


class TranscriptRing(OutputRing):
    """Raw offsets stay authoritative; cleaned chunks have independently bounded storage."""
    def __init__(self, capacity=4 * 1024 * 1024):
        super().__init__(capacity)
        self.sanitizer = TranscriptSanitizer()
        self.clean = deque()
        self.clean_size = 0
        self.clean_start = 0

    def append(self, chunk):
        start = self.end
        super().append(chunk)
        projected = self.sanitizer.feed(chunk)
        if projected:
            self.clean.append((start, self.end, projected))
            self.clean_size += len(projected)
        while self.clean and (self.clean_size > self.capacity or len(self.clean) > 8192
                              or self.clean[0][0] < self.start):
            _, end, removed = self.clean.popleft()
            self.clean_start = max(self.clean_start, end)
            self.clean_size -= len(removed)
        self.clean_start = max(self.clean_start, self.start)

    def selected_text(self, since=None):
        requested = self.clean_start if since is None else since
        require(type(requested) is int and 0 <= requested <= self.end, "invalid_output_offset")
        rows, lost = [], requested < self.clean_start
        for start, end, content in self.clean:
            if end <= requested:
                continue
            if start < requested:
                # Do not pretend an arbitrary offset inside a cleaned chunk is exact.
                lost = True
                continue
            rows.append(content)
        rows.append(self.sanitizer.pending_marker())
        return b"".join(rows), lost


@dataclass(frozen=True)
class LocalReview:
    id: str
    revision: int
    direction: str
    session: str
    text: str
    payload: bytes
    final_return: bool
    lost_prefix: bool = False
    bridge_binding: tuple | None = None
    proposal_id: str | None = None
    expires_at: float | None = None
    edited: bool = False

    @property
    def presentation(self):
        # JSON string notation distinguishes backslashes, LF, Unicode and controls.
        return json.dumps(self.text, ensure_ascii=True)


@dataclass(frozen=True)
class AppSnapshot:
    connection: str
    session: str | None
    inbox: tuple
    review: LocalReview | None
    review_state: str
    paused: bool
    output_start: int
    output_end: int
    preferences: str
    approved_outputs: tuple
    last_error: str | None
    final_return: bool
    output_mode: str
    last_transfer_offset: int | None
    bridge: object = None
    command_risk: object = None


BLOCK_SIZES = ("SINGLE", "SMALL", "MEDIUM", "LARGE")
OUTPUT_MODES = {
    "LAST_1": "Letzte Zeile",
    "SINCE_LAST": "Seit letztem Command",
    "LAST_20": "Letzte 20", "LAST_50": "Letzte 50",
    "LAST_100": "Letzte 100", "LAST_500": "Letzte 500",
}


class PreferenceStore:
    """Small, atomic preferences file. Never stores approvals or terminal text."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def _directory_fd(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not (info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700):
            os.close(fd)
            raise ProbeError("unsafe_preferences_directory")
        return fd

    def load(self):
        directory_fd = None
        try:
            directory_fd = self._directory_fd()
            try:
                fd = os.open("preferences.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            except FileNotFoundError:
                return True, "SMALL"
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and stat.S_IMODE(info.st_mode) == 0o600, "unsafe_preferences_file")
                raw = source.read(4097)
            require(len(raw) <= 4096, "preferences_limit")
            value = decode_packet(raw)
            fields(value, {"version", "final_return", "block_size"})
            require(type(value["version"]) is int and value["version"] == 1, "preferences_version")
            require(type(value["final_return"]) is bool and value["block_size"] in BLOCK_SIZES,
                    "invalid_preferences")
            return value["final_return"], value["block_size"]
        except OSError:
            raise ProbeError("preferences_read_failure") from None
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def save(self, final_return, block_size):
        require(type(final_return) is bool and block_size in BLOCK_SIZES, "invalid_preferences")
        self.load()  # Reject a symlink, unsafe permissions, or malformed existing file.
        directory_fd = None
        temporary = ".preferences-" + uuid.uuid4().hex
        try:
            directory_fd = self._directory_fd()
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as target:
                target.write(json.dumps({"version": 1, "final_return": final_return,
                                         "block_size": block_size}, ensure_ascii=True).encode("ascii"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, "preferences.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except OSError:
            raise ProbeError("preferences_write_failure") from None
        finally:
            if directory_fd is not None:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(directory_fd)


def preference_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Admin in the Loop"
    return Path.home() / ".config" / "admin-in-the-loop"


def review_fragments(text):
    """Presentation only: taggable runs, never used to construct PTY payloads."""
    runs = []
    plain = []

    def flush():
        if plain:
            runs.append(("".join(plain), "plain"))
            plain.clear()

    for char in text:
        if char == "\n":
            flush()
            runs.append(("↵\n", "control"))
        elif char == "↵" or unicodedata.category(char).startswith(("C", "Z")) and char != " ":
            flush()
            runs.append(("⟦U+%04X⟧" % ord(char), "control"))
        elif ord(char) > 127:
            flush()
            runs.append(("⟦U+%04X %s⟧" % (ord(char), char), "unicode"))
        else:
            plain.append(char)
    flush()
    return tuple(runs)


import http.client
import ipaddress
import queue
import ssl
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlencode, urljoin
from dataclasses import field, replace


RISK_TIMEOUT = 2.0
RISK_STDOUT_LIMIT = 1024 * 1024
RISK_STDERR_LIMIT = 16 * 1024
# JSON enums from sh-guard/types.rs at 92005d65b1232946924b524bcad267a29d8a2cb5.
RISK_FACTORS = frozenset((
    "recursive_delete", "force_flag", "broad_scope", "secrets_exposure",
    "network_exfiltration", "pipe_to_execution", "command_substitution",
    "process_substitution", "untrusted_execution", "privilege_escalation",
    "path_injection", "git_history_destruction", "escapes_project_boundary",
    "shell_injection", "zsh_module_loading", "zsh_glob_execution",
    "obfuscated_command", "obfuscated_exfiltration", "command_execution", "write"))
RISK_INTENTS = frozenset(("read", "write", "delete", "execute", "network",
                         "process_control", "privilege", "package_install",
                         "git_mutation", "env_modify", "search", "info"))
RISK_CAPABILITIES = frozenset(("shell", "command", "reverse_shell", "bind_shell",
                              "file_read", "file_write", "upload", "download",
                              "library_load", "privilege_escalation"))
RISK_LEVELS = ("safe", "caution", "danger", "critical")


def _risk_fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "risk_schema")


def _risk_number(value, low=0, high=100):
    require(type(value) is int and low <= value <= high, "risk_number")


def _risk_enum(value, choices):
    require(type(value) is str and value in choices, "risk_enum")


def _risk_enum_list(value, choices):
    require(type(value) is list, "risk_schema")
    for item in value:
        _risk_enum(item, choices)


def _risk_text(value, nullable=False):
    require(type(value) is str or nullable and value is None, "risk_schema")


def _risk_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "risk_duplicate_key")
        result[key] = value
    return result


def _risk_bad_number(_value):
    raise ProbeError("risk_number")


def _risk_json_bounds(value, depth=0):
    require(depth <= 32, "risk_json_limit")
    if type(value) is str:
        require(len(value.encode("utf-8", errors="strict")) <= 8192, "risk_json_limit")
    elif type(value) is list:
        require(len(value) <= 512, "risk_json_limit")
        for item in value:
            _risk_json_bounds(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            _risk_json_bounds(key, depth + 1)
            _risk_json_bounds(item, depth + 1)


def _risk_freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _risk_freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_risk_freeze(item) for item in value)
    return value


def validate_command_risk(raw, command, exit_code):
    """Version-bound serde contract; no authorization and no content in errors."""
    require(type(raw) is bytes and len(raw) <= RISK_STDOUT_LIMIT, "risk_output_limit")
    try:
        text = raw.decode("utf-8", errors="strict")
        # Bound nesting BEFORE json.loads, without counting braces inside strings.
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                require(depth <= 32, "risk_json_limit")
            elif char in "]}":
                depth -= 1
        value = json.loads(text, object_pairs_hook=_risk_json_pairs,
                           parse_constant=_risk_bad_number, parse_float=_risk_bad_number)
        _risk_json_bounds(value)
    except (UnicodeError, ValueError, RecursionError):
        raise ProbeError("risk_json") from None
    _risk_fields(value, "command score level quick_decision reason risk_factors "
                        "sub_commands pipeline_flow mitre_mappings parse_confidence")
    require(value["command"] == command and type(value["command"]) is str, "risk_command")
    _risk_number(value["score"])
    level = RISK_LEVELS[sum(value["score"] > boundary for boundary in (20, 50, 80))]
    require(value["level"] == level and type(exit_code) is int
            and exit_code == RISK_LEVELS.index(level), "risk_exit_level")
    _risk_enum(value["quick_decision"], ("safe", "risky", "blocked"))
    _risk_text(value["reason"])
    _risk_enum_list(value["risk_factors"], RISK_FACTORS)
    _risk_enum(value["parse_confidence"], ("full", "partial", "fallback"))
    require(type(value["sub_commands"]) is list, "risk_schema")
    for sub in value["sub_commands"]:
        _risk_fields(sub, "command executable intent targets flags score risk_factors "
                          "reversibility capabilities")
        _risk_text(sub["command"])
        _risk_text(sub["executable"], nullable=True)
        _risk_enum_list(sub["intent"], RISK_INTENTS)
        _risk_enum_list(sub["capabilities"], RISK_CAPABILITIES)
        _risk_enum_list(sub["risk_factors"], RISK_FACTORS)
        _risk_number(sub["score"])
        _risk_enum(sub["reversibility"], ("reversible", "hard_to_reverse", "irreversible"))
        require(type(sub["targets"]) is list and type(sub["flags"]) is list, "risk_schema")
        for target in sub["targets"]:
            _risk_fields(target, "path scope sensitivity")
            _risk_text(target["path"], nullable=True)
            _risk_enum(target["scope"], ("none", "single_file", "directory",
                                       "directory_recursive", "system", "home", "root"))
            _risk_enum(target["sensitivity"], ("normal", "config", "system", "secrets", "protected"))
        for flag in sub["flags"]:
            _risk_fields(flag, "flag modifier risk_factor description")
            _risk_text(flag["flag"])
            _risk_number(flag["modifier"], -128, 127)
            _risk_enum(flag["risk_factor"], RISK_FACTORS)
            _risk_text(flag["description"])
    pipeline = value["pipeline_flow"]
    if pipeline is not None:
        _risk_fields(pipeline, "flow_type taint_flows composite_score")
        _risk_enum(pipeline["flow_type"], ("pipe", "and", "or", "sequence", "mixed"))
        _risk_number(pipeline["composite_score"])
        require(type(pipeline["taint_flows"]) is list, "risk_schema")
        for flow in pipeline["taint_flows"]:
            _risk_fields(flow, "source propagators sink escalation escalation_reason")
            for name, variants, path_variant in (
                    ("source", ("sensitive_file", "environment_var", "command_output", "stdin"), "sensitive_file"),
                    ("sink", ("network_send", "file_write", "execution", "display"), "file_write")):
                tagged = flow[name]
                require(type(tagged) is dict, "risk_schema")
                _risk_enum(tagged.get("type"), variants)
                _risk_fields(tagged, "type path" if tagged["type"] == path_variant else "type")
                if "path" in tagged:
                    _risk_text(tagged["path"])
            require(type(flow["propagators"]) is list, "risk_schema")
            for prop in flow["propagators"]:
                if type(prop) is str:
                    _risk_enum(prop, ("passthrough", "filtering", "aggregation"))
                else:
                    _risk_fields(prop, "encoding")
                    _risk_fields(prop["encoding"], "method")
                    _risk_text(prop["encoding"]["method"])
            _risk_number(flow["escalation"], 0, 255)
            _risk_text(flow["escalation_reason"])
    require(type(value["mitre_mappings"]) is list, "risk_schema")
    for mapping in value["mitre_mappings"]:
        _risk_fields(mapping, "technique_id technique_name tactic")
        for item in mapping.values():
            _risk_text(item)
    return _risk_freeze(value)


@dataclass(frozen=True)
class CommandRiskTool:
    """Local caller-supplied descriptor. No manifest, discovery or pin admission."""
    binary: str
    sha256: str
    size: int
    version: str
    generation: int
    lease_path: str | None = None

    def __post_init__(self):
        require(type(self.binary) is str and Path(self.binary).is_absolute(), "risk_binary_path")
        require(type(self.sha256) is str and re.fullmatch("[0-9a-f]{64}", self.sha256), "risk_binary_hash")
        _risk_number(self.size, 1, 16 * 1024 * 1024)
        _risk_number(self.generation, 0, 2**63 - 1)
        require(self.lease_path is None or type(self.lease_path) is str
                and Path(self.lease_path).is_absolute(), "risk_install_lock")
        require(type(self.version) is str and len(self.version) <= 64
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", self.version),
                "risk_version")


@dataclass(frozen=True)
class CommandRiskKey:
    epoch: int
    tool: CommandRiskTool
    review_id: str
    revision: int
    direction: str
    session: str
    bridge_binding: tuple | None
    proposal_id: str | None
    expires_at: float | None
    final_return: bool
    payload_sha256: str


@dataclass(frozen=True)
class CommandRiskJob:
    key: CommandRiskKey
    review: LocalReview = field(repr=False)
    directory: str


@dataclass(frozen=True)
class CommandRiskResult:
    key: CommandRiskKey
    state: str
    error: str | None = None
    data: object = field(default=None, repr=False)
    analyzed_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class CommandRiskSnapshot:
    state: str
    result: CommandRiskResult | None = None
    error: str | None = None


def _risk_review_valid(review):
    try:
        return (type(review) is LocalReview and review.direction == "TO_TERMINAL"
                and type(review.payload) is bytes
                and normalize_command(review.text, review.final_return) == review.payload
                and review.text.encode("utf-8", errors="strict") == review.payload)
    except (ProbeError, UnicodeError):
        return False


def _risk_directory(path):
    require(Path(path).is_absolute() and str(Path(path).resolve()) == str(path), "risk_directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "risk_directory")
    finally:
        os.close(fd)


def _risk_verify_tool(tool, cancel):
    require(str(Path(tool.binary).resolve()) == tool.binary, "risk_binary_path")
    _risk_directory(str(Path(tool.binary).parent))
    # A substituted FIFO must fail fstat, not block before the process deadline.
    fd = os.open(tool.binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o500 and info.st_size == tool.size,
                "risk_binary_integrity")
        digest = hashlib.sha256()
        remaining = tool.size
        while remaining:
            require(not cancel.is_set(), "risk_cancelled")
            chunk = source.read(min(65536, remaining))
            require(bool(chunk), "risk_binary_integrity")
            remaining -= len(chunk)
            digest.update(chunk)
        require(not source.read(1) and digest.hexdigest() == tool.sha256, "risk_binary_integrity")


def _risk_reap(process):
    # Kill the group even if its leader exited: descendants may hold our pipes.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.25 if sig == signal.SIGTERM else 1.0)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise ProbeError("risk_cleanup_failed") from None


def _risk_process(argv, directory, environment, rules_fd, cancel):
    require(not cancel.is_set(), "risk_cancelled")
    deadline = time.monotonic() + RISK_TIMEOUT
    process = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=directory, env=environment, close_fds=True,
                               pass_fds=(rules_fd,), start_new_session=True)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            for stream, limit in ((process.stdout, RISK_STDOUT_LIMIT),
                                  (process.stderr, RISK_STDERR_LIMIT)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, limit)
            while selector.get_map() or process.poll() is None:
                require(not cancel.is_set(), "risk_cancelled")
                remaining = deadline - time.monotonic()
                require(remaining > 0, "risk_timeout")
                for key, _mask in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, key.data + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stderr:
                        # No warning is compatible with the rules-isolation contract.
                        raise ProbeError("risk_stderr")
                    else:
                        require(len(output) + len(chunk) <= key.data, "risk_output_limit")
                        output.extend(chunk)
            require(not cancel.is_set(), "risk_cancelled")
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            require(code in (0, 1, 2, 3), "risk_process_exit")
            return bytes(output), code
    finally:
        try:
            _risk_reap(process)
        finally:
            process.stdout.close()
            process.stderr.close()


RISK_LEASE_TIMEOUT = 2.0


def _risk_acquire_lease(path, cancel, exclusive=False, create=False, timeout=RISK_LEASE_TIMEOUT):
    if path is None:
        return None  # Private caller-created test tools do not share an install.
    _risk_directory(str(Path(path).parent))
    require(Path(path).resolve() == Path(path), "risk_install_lock")
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "risk_install_lock")
        deadline = time.monotonic() + timeout
        while True:
            require(not cancel.is_set(), "risk_cancelled")
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                require(time.monotonic() < deadline, "risk_lease_busy")
                cancel.wait(min(0.02, max(0, deadline - time.monotonic())))
    except BaseException:
        os.close(fd)
        raise


def run_command_risk(job, cancel):
    lease = None
    try:
        lease = _risk_acquire_lease(job.key.tool.lease_path, cancel)
        return _run_command_risk_leased(job, cancel)
    except ProbeError as exc:
        return CommandRiskResult(job.key, "UNAVAILABLE", str(exc))
    except Exception:
        return CommandRiskResult(job.key, "UNAVAILABLE", "risk_process_failure")
    finally:
        if lease is not None:
            os.close(lease)  # Only after every subprocess has exited and been reaped.


def _run_command_risk_leased(job, cancel):
    """Only text analysis. Errors are bounded metadata, never argv/stderr contents."""
    try:
        require(_risk_review_valid(job.review)
                and hashlib.sha256(job.review.payload).hexdigest() == job.key.payload_sha256,
                "risk_snapshot")
        require((job.review.id, job.review.revision, job.review.direction, job.review.session,
                 job.review.bridge_binding, job.review.proposal_id, job.review.expires_at,
                 job.review.final_return) ==
                (job.key.review_id, job.key.revision, job.key.direction, job.key.session,
                 job.key.bridge_binding, job.key.proposal_id, job.key.expires_at,
                 job.key.final_return), "risk_snapshot")
        _risk_verify_tool(job.key.tool, cancel)
        _risk_directory(job.directory)
        with tempfile.TemporaryDirectory(prefix="analysis-", dir=job.directory) as directory:
            for name in ("home", "tmp"):
                Path(directory, name).mkdir(mode=0o700)
            rules = str(Path(directory, "empty-rules"))
            writer = os.open(rules, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(writer)
            rules_fd = os.open(rules, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(rules_fd)
                require(stat.S_ISREG(info.st_mode) and info.st_size == 0
                        and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                        "risk_rules")
                os.unlink(rules)
                environment = {"HOME": str(Path(directory, "home")),
                               "TMPDIR": str(Path(directory, "tmp")), "PATH": "/usr/bin:/bin",
                               "LANG": "C", "LC_ALL": "C", "TERM": "dumb", "NO_COLOR": "1"}
                binary = job.key.tool.binary
                version, code = _risk_process([binary, "--version"], directory, environment, rules_fd, cancel)
                require(code == 0 and version == ("sh-guard " + job.key.tool.version + "\n").encode("ascii"),
                        "risk_version")
                _risk_verify_tool(job.key.tool, cancel)
                raw, code = _risk_process([binary, "--json", "--shell", "bash", "--rules",
                                           "/dev/fd/" + str(rules_fd), "--", job.review.text],
                                          directory, environment, rules_fd, cancel)
                data = validate_command_risk(raw, job.review.text, code)
                require(not cancel.is_set(), "risk_cancelled")
                state = "RESULT" if data["parse_confidence"] == "full" else "UNKNOWN"
                return CommandRiskResult(job.key, state, data=data)
            finally:
                os.close(rules_fd)
    except ProbeError as exc:
        return CommandRiskResult(job.key, "UNAVAILABLE", str(exc))
    except Exception:
        return CommandRiskResult(job.key, "UNAVAILABLE", "risk_process_failure")


def command_risk_spans(text, data):
    """All-or-none exact CHARACTER intervals for a deliberately closed shell subset.

    No execution, shell equivalence, substring searching or parser-confidence claim.
    The UI separately projects these source characters to actual Tk indices.
    """
    if not data or data["parse_confidence"] != "full" or data["command"] != text:
        return ()
    intervals, start, end, quote, word, need_command = [], None, None, None, "", False
    reserved = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
                "case", "esac", "in", "function", "select", "time", "coproc", "!", "[[", "]]"}

    def finish_word():
        return word not in reserved

    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                quote = None
            elif quote == '"' and char in "$`":
                return ()
            elif quote == '"' and char == "\\":
                if i + 1 == len(text) or text[i + 1] == "\n":
                    return ()
                i += 1
            end = i + 1
        elif char == "\\":
            if i + 1 == len(text) or text[i + 1] == "\n":
                return ()
            start = i if start is None else start
            word += text[i:i + 2]
            i += 1
            end, need_command = i + 1, False
        elif char in "'\"":
            start = i if start is None else start
            quote, word, end, need_command = char, word + char, i + 1, False
        elif char in "$`<>(){}" or char == "\t":
            return ()
        elif char == "#" and not word:
            if start is not None:
                intervals.append((start, end))
                start = end = None
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline
            continue
        elif char == " ":
            if not finish_word():
                return ()
            word = ""
        elif char in ";\n|&":
            if not finish_word():
                return ()
            operator = char
            if char in "|&" and i + 1 < len(text) and text[i:i + 2] in ("||", "&&", "|&"):
                operator = text[i:i + 2]
                i += 1
            if operator == "&" or start is None and (operator != "\n" or need_command):
                return ()
            if start is not None:
                intervals.append((start, end))
            start = end = None
            word = ""
            need_command = operator in ("|", "|&", "&&", "||")
        else:
            start = i if start is None else start
            word += char
            end, need_command = i + 1, False
        i += 1
    if quote or need_command or not finish_word():
        return ()
    if start is not None:
        intervals.append((start, end))
    subs = data["sub_commands"]
    if len(intervals) != len(subs) or not intervals:
        return ()
    if any(text[left:right] != sub["command"] for (left, right), sub in zip(intervals, subs)):
        return ()
    return tuple((left, right, sub["score"]) for (left, right), sub in zip(intervals, subs))


@dataclass(frozen=True)
class CommandRiskRelease:
    version: str
    target: str
    archive_sha256: str
    archive_size: int
    binary_sha256: str
    binary_size: int
    approved: bool = False

    @property
    def url(self):
        return ("https://github.com/aryanbhosale/sh-guard/releases/download/v" + self.version
                + "/sh-guard-" + self.target + ".tar.gz")

    def validate(self):
        require(type(self.approved) is bool and self.approved, "risk_pin_not_approved")
        require(self.target in RISK_RELEASES, "risk_platform")
        CommandRiskTool("/sh-guard", self.binary_sha256, self.binary_size, self.version, 0)
        require(re.fullmatch("[0-9a-f]{64}", self.archive_sha256), "risk_archive_hash")
        _risk_number(self.archive_size, 1, 16 * 1024 * 1024)


# Reviewed hashes are candidates, not artifact/SBOM/offline/legal/native admission.
RISK_RELEASES = MappingProxyType({release.target: release for release in (
    CommandRiskRelease("0.1.10", "x86_64-unknown-linux-gnu",
        "7c1a34a26e5eb4ab58bfc4f879ba6bca366c1df8e3a568e2b77198a92fd83d9f", 2366483,
        "755e6e865bf2501cbae618988aced28ea24376f992219c96072b0b5874c4b2ae", 5622640),
    CommandRiskRelease("0.1.10", "aarch64-unknown-linux-gnu",
        "772a53bb5f736462b21e1e643006fc2d6abbd31ef93f575a030e3d86b2daeee3", 2289546,
        "295c0818ee4eaac24741e5ea392768c8807b1112b294305f611e5fecbd2494dd", 5493288),
    CommandRiskRelease("0.1.10", "x86_64-apple-darwin",
        "513dd61dd34d739b240011f9d620ec3fb6b7d11524d2c1fd7b9545ca795e15ad", 2237864,
        "7a6e2903dc529bbf5b4b43b50ae761924a011002492f46176ee7195caa4c92a9", 4900876),
    CommandRiskRelease("0.1.10", "aarch64-apple-darwin",
        "56a4e91df78ac11eacb046885ca7266da965ca76e5be6c908127dd5333fd16c0", 2154488,
        "3c74dfff45c4c4b892d9d31889d0c2f5ac30ef0c2f9061115d2cccc00ef42d16", 4744832))})


def command_risk_release():
    machine = platform.machine().lower()
    arch = "x86_64" if machine in ("x86_64", "amd64") else "aarch64" if machine in ("arm64", "aarch64") else None
    suffix = "apple-darwin" if sys.platform == "darwin" else "unknown-linux-gnu" if sys.platform.startswith("linux") else None
    require(arch is not None and suffix is not None, "risk_platform")
    return RISK_RELEASES[arch + "-" + suffix]


RISK_DOWNLOAD_TIMEOUT = 120.0
RISK_ARCHIVE_EXPANDED_LIMIT = 32 * 1024 * 1024
# Shared by every request/controller in this process. close()/timeout does NOT
# release admission while libc is still resolving; no persistent resolver service.
_RISK_RESOLVER_ADMISSION = threading.BoundedSemaphore(1)


class _RiskDownloadBudget:
    """Request-local deadline/watchdog; never changes global socket defaults."""
    def __init__(self, cancel):
        self.cancel = cancel
        self.deadline = time.monotonic() + RISK_DOWNLOAD_TIMEOUT
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []
        self.resolvers = []
        self.error = None
        self.thread = threading.Thread(target=self._watch, name="aitl-risk-download-budget", daemon=True)
        self.thread.start()

    def check(self):
        if self.cancel.is_set():
            raise ProbeError("risk_cancelled")
        require(time.monotonic() < self.deadline and self.error is None, "risk_download_timeout")

    def timeout(self):
        self.check()
        return max(0.001, min(15.0, self.deadline - time.monotonic()))

    def register(self, sock):
        with self.lock:
            self.sockets.append(sock)
            self.check()

    def _abort_sockets(self):
        with self.lock:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def _watch(self):
        while not self.done.wait(0.02):
            if self.cancel.is_set() or time.monotonic() >= self.deadline:
                self.error = "risk_cancelled" if self.cancel.is_set() else "risk_download_timeout"
                self._abort_sockets()  # Interrupt headers, TLS and chunk framing too.
                return

    def resolve(self, host, port):
        # libc DNS cannot be force-cancelled in Python. Its helper returns only data:
        # after cancellation no late result is permitted to connect a socket.
        self.check()
        if not _RISK_RESOLVER_ADMISSION.acquire(blocking=False):
            self.check()  # Cancel/deadline takes precedence over busy.
            raise ProbeError("risk_download_dns_busy")
        thread = None
        try:
            result = queue.Queue(maxsize=1)

            def resolve():
                try:
                    try:
                        value = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
                    except Exception as exc:
                        value = exc
                    result.put_nowait(value)
                finally:
                    _RISK_RESOLVER_ADMISSION.release()  # Actual resolver return only.

            thread = threading.Thread(target=resolve, name="aitl-risk-download-dns", daemon=True)
            self.resolvers.append(thread)
            self.check()
            thread.start()
        except BaseException:
            if thread in self.resolvers:
                self.resolvers.remove(thread)
            _RISK_RESOLVER_ADMISSION.release()
            raise
        while True:
            self.check()
            try:
                value = result.get(timeout=min(0.02, self.timeout()))
                self.check()
                if isinstance(value, Exception):
                    raise ProbeError("risk_download_dns") from None
                return value
            except queue.Empty:
                pass

    def close(self):
        self.done.set()
        self.thread.join()
        self._abort_sockets()
        for thread in self.resolvers:
            thread.join(timeout=0.02)


def _risk_download_handler(budget):
    from urllib.request import HTTPSHandler

    class Connection(http.client.HTTPSConnection):
        def connect(self):
            require(self._tunnel_host is None, "risk_download_proxy")
            last_error = None
            for family, kind, proto, _name, address in budget.resolve(self.host, self.port):
                budget.check()
                raw = socket.socket(family, kind, proto)
                budget.register(raw)
                try:
                    raw.setblocking(False)
                    code = raw.connect_ex(address)
                    require(code in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY), "risk_download_connect")
                    while code:
                        budget.check()
                        _, writable, failed = select.select([], [raw], [raw], min(0.02, budget.timeout()))
                        if writable or failed:
                            code = raw.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                            require(code == 0, "risk_download_connect")
                    raw.settimeout(budget.timeout())
                    secured = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                    budget.register(secured)
                    secured.do_handshake()
                    budget.check()
                    self.sock = secured
                    return
                except (OSError, ProbeError) as exc:
                    raw.close()
                    budget.check()
                    last_error = exc
            raise ProbeError("risk_download_connect") from last_error

    class Handler(HTTPSHandler):
        def https_open(self, req):
            budget.check()
            value = self.do_open(Connection, req, context=self._context)
            try:
                budget.check()
            except BaseException:
                value.close()  # do_open produced a response, but no caller owns it yet.
                raise
            return value

    return Handler(context=ssl.create_default_context())


def download_command_risk(release, cancel):
    """Explicit installer path ONLY; all production entries are currently gated."""
    from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
    from urllib.error import HTTPError
    release.validate()
    hosts = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}

    class Redirects(HTTPRedirectHandler):
        count = 0

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            budget.check()
            self.count += 1
            parsed = urlsplit(newurl)
            require(self.count <= 5 and parsed.scheme == "https" and parsed.hostname in hosts
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password,
                    "risk_download_redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

        def http_error_302(self, req, fp, code, msg, headers):
            try:
                location = headers.get("Location")
                require(type(location) is str and len(location) <= 8192, "risk_download_redirect")
                redirected = self.redirect_request(req, fp, code, msg, headers, urljoin(req.full_url, location))
                budget.check()
            finally:
                fp.close()  # Includes every validation/budget/cancel failure.
            return self.parent.open(redirected, timeout=budget.timeout())

        http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    budget, raw = _RiskDownloadBudget(cancel), bytearray()
    try:
        handler = _risk_download_handler(budget)
        budget.check()
        with build_opener(ProxyHandler({}), Redirects(), handler).open(release.url, timeout=budget.timeout()) as response:
            budget.check()
            while True:
                budget.check()
                chunk = response.read1(65536)  # At most one underlying receive.
                budget.check()  # Includes EOF, never accept a late success.
                if not chunk:
                    break
                require(len(raw) + len(chunk) <= release.archive_size, "risk_archive_size")
                raw.extend(chunk)
        budget.check()
        return bytes(raw)
    except Exception as exc:
        try:
            if isinstance(exc, HTTPError):
                exc.close()  # Retained exceptions/tracebacks must not retain live FDs.
        finally:
            budget.check()  # Deadline/cancel wins over the induced socket error.
        raise
    finally:
        budget.close()


def _risk_archive_binary(raw, release, cancel):
    """Bound ALL physical bytes before interpreting any tar header/metadata."""
    expanded = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        while True:
            require(not cancel.is_set(), "risk_cancelled")
            chunk = compressed.read(min(65536, RISK_ARCHIVE_EXPANDED_LIMIT + 1 - len(expanded)))
            if not chunk:
                break
            expanded.extend(chunk)
            require(len(expanded) <= RISK_ARCHIVE_EXPANDED_LIMIT, "risk_archive_size")
    require(len(expanded) % 512 == 0, "risk_archive_members")
    offset, seen, binary = 0, set(), None
    while offset + 512 <= len(expanded):
        require(not cancel.is_set(), "risk_cancelled")
        block = bytes(expanded[offset:offset + 512])
        if block == b"\0" * 512:
            require(len(expanded) - offset >= 1024 and not any(expanded[offset:]), "risk_archive_members")
            break
        # Reject GNU longname/longlink, PAX, sparse, devices and extensions BEFORE
        # tarfile gets a chance to consume or expand those headers.
        require(block[156:157] in (tarfile.REGTYPE, tarfile.AREGTYPE), "risk_archive_members")
        try:
            member = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        except (tarfile.TarError, UnicodeError, ValueError):
            raise ProbeError("risk_archive_members") from None
        require(member.name in ("sh-guard", "sh-guard-mcp") and member.name not in seen, "risk_archive_members")
        _risk_number(member.size, 1, 16 * 1024 * 1024)
        seen.add(member.name)
        begin = offset + 512
        offset = begin + ((member.size + 511) // 512) * 512
        require(offset <= len(expanded), "risk_archive_size")
        if member.name == "sh-guard":
            require(member.size == release.binary_size, "risk_binary_integrity")
            binary = bytes(expanded[begin:begin + member.size])
    else:
        raise ProbeError("risk_archive_members")
    require(seen == {"sh-guard", "sh-guard-mcp"}, "risk_archive_members")
    require(hashlib.sha256(binary).hexdigest() == release.binary_sha256, "risk_binary_integrity")
    return binary


def _risk_private_tree(base, parts):
    """Create only private owned components, relative to a checked config root."""
    require(Path(base).is_absolute() and Path(base).resolve() == Path(base), "risk_directory")
    fd = PreferenceStore(base)._directory_fd()
    current = Path(base)
    try:
        for name in parts:
            require(re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name not in (".", ".."), "risk_directory")
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            info = os.fstat(child)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                os.close(child)
                raise ProbeError("risk_directory")
            os.close(fd)
            fd, current = child, current / name
        _risk_directory(str(current))
        return current
    finally:
        os.close(fd)


def _risk_quarantine(path):
    if sys.platform == "darwin":
        value = ("0083;%x;Admin in the Loop;%s" % (int(time.time()), uuid.uuid4())).encode("ascii")
        os.setxattr(path, "com.apple.quarantine", value, follow_symlinks=False)
        require(os.getxattr(path, "com.apple.quarantine", follow_symlinks=False) == value, "risk_quarantine")


def _risk_probe(tool, work, cancel):
    text = ":\n"
    review = LocalReview("risk-start-probe", 0, "TO_TERMINAL", "risk-start-probe", text, text.encode(), True)
    key = CommandRiskKey(0, tool, review.id, 0, review.direction, review.session, None, None, None,
                         True, hashlib.sha256(review.payload).hexdigest())
    result = run_command_risk(CommandRiskJob(key, review, str(work)), cancel)
    require(result.state == "RESULT", result.error or "risk_probe")


class CommandRiskInstallation:
    """An approved manifest is mandatory before filesystem, transport or execution."""
    def __init__(self, base, release):
        self.base, self.release = Path(base), release

    @property
    def destination(self):
        return self.base / "tools" / "sh-guard" / self.release.version / self.release.target / "sh-guard"

    def tool(self, path=None):
        release = self.release
        return CommandRiskTool(str(path or self.destination), release.binary_sha256,
                               release.binary_size, release.version, 1,
                               None if path is not None else str(self.base / "tools" / "sh-guard" / ".lock"))

    def check(self, cancel):
        self.release.validate()
        require(self.base.is_absolute() and str(self.base.resolve()) == str(self.base), "risk_directory")
        if not self.destination.exists() and not self.destination.is_symlink():
            return None
        for path in (self.base, *self.destination.parent.parents):
            if path == self.base or path.is_relative_to(self.base):
                _risk_directory(str(path))
        tool = self.tool()
        lock = _risk_acquire_lease(tool.lease_path, cancel)
        try:
            _risk_verify_tool(tool, cancel)
            work = _risk_private_tree(self.base, ("tools", "sh-guard", ".work"))
            _risk_probe(tool, work, cancel)
            return tool
        finally:
            os.close(lock)

    def install(self, cancel, progress, transport=download_command_risk):
        self.release.validate()
        release = self.release
        root = _risk_private_tree(self.base, ("tools", "sh-guard"))
        work = _risk_private_tree(self.base, ("tools", "sh-guard", ".work"))
        lock = _risk_acquire_lease(str(root / ".lock"), cancel, exclusive=True, create=True)
        try:
            progress("DOWNLOADING")
            require(not cancel.is_set(), "risk_cancelled")
            raw = transport(release, cancel)
            require(type(raw) is bytes and len(raw) == release.archive_size, "risk_archive_size")
            require(hashlib.sha256(raw).hexdigest() == release.archive_sha256, "risk_archive_hash")
            progress("VERIFYING")
            with tempfile.TemporaryDirectory(prefix="install-", dir=work) as staging:
                binary = Path(staging, "sh-guard")
                payload = _risk_archive_binary(raw, release, cancel)
                fd = os.open(binary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                _risk_quarantine(binary)
                binary.chmod(0o500)
                require(not cancel.is_set(), "risk_cancelled")
                progress("PROBING")
                _risk_probe(self.tool(binary), work, cancel)
                parent = _risk_private_tree(self.base, ("tools", "sh-guard", release.version))
                target = parent / release.target
                # An existing install is never overwritten; upgrade publishes a new version.
                require(not target.exists() and not target.is_symlink(), "risk_install_target")
                receipt = Path(staging, "receipt.json")
                fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as output:
                    json.dump({"version": release.version, "target": release.target, "source": release.url,
                               "archive_sha256": release.archive_sha256, "binary_sha256": release.binary_sha256,
                               "installed_at": time.time()}, output)
                    output.flush()
                    os.fsync(output.fileno())
                require(not cancel.is_set(), "risk_cancelled")
                os.rename(staging, target)  # Binary + receipt become visible together.
                return self.tool(), str(work)
        finally:
            os.close(lock)

    def remove(self):
        self.release.validate()
        root = self.base / "tools" / "sh-guard"
        _risk_directory(str(root))
        # Remove is a foreground action: fail busy immediately rather than block Tk.
        lock = _risk_acquire_lease(str(root / ".lock"), threading.Event(), exclusive=True, timeout=0)
        try:
            tool = self.tool()
            _risk_verify_tool(tool, threading.Event())
            target = self.destination.parent
            require(set(p.name for p in target.iterdir()) == {"sh-guard", "receipt.json"}, "risk_cleanup_incomplete")
            receipt = target / "receipt.json"
            info = receipt.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600, "risk_cleanup_incomplete")
            self.destination.unlink()
            receipt.unlink()
            target.rmdir()
        finally:
            os.close(lock)


class CommandRiskSetup:
    """One bounded setup operation; only the owner consumes its data event."""
    def __init__(self, installation, install=False, transport=download_command_risk):
        self.cancel = threading.Event()
        self.events = queue.Queue(maxsize=1)
        self.phase = "INSTALLING" if install else "CHECKING_TOOL"

        def run():
            try:
                if install:
                    value = installation.install(self.cancel, lambda phase: None, transport)
                else:
                    tool = installation.check(self.cancel)
                    value = None if tool is None else (tool, str(tool_path_work(installation)))
                self.events.put_nowait(("ready" if value else "missing", value))
            except Exception as exc:
                self.events.put_nowait(("error", str(exc) if isinstance(exc, ProbeError) else "risk_setup_failure"))

        self.thread = threading.Thread(target=run, name="aitl-risk-setup", daemon=True)
        self.thread.start()

    def close(self):
        self.cancel.set()


def tool_path_work(installation):
    return installation.base / "tools" / "sh-guard" / ".work"


class CommandRiskReviewer:
    """One data-only worker; observation and publication belong to its owner thread."""
    def __init__(self):
        self.owner = threading.get_ident()
        self.enabled = False
        self.epoch = 0
        self.tool = None
        self.directory = None
        self.state = "DISABLED"
        self.result = None
        self.current = None
        self.events = queue.Queue(maxsize=4)
        self.condition = threading.Condition()
        self.pending = None
        self.cancel = None
        self.stopped = False
        self.thread = None

    def _owner(self):
        require(threading.get_ident() == self.owner, "risk_wrong_thread")

    def quiescent(self):
        self._owner()
        with self.condition:
            return self.pending is None and self.cancel is None

    def invalidate(self):
        self._owner()
        with self.condition:
            self.pending = None
            if self.cancel:
                self.cancel.set()
        self.current = self.result = None
        self.state = "STALE" if self.enabled else "DISABLED"

    def configure(self, tool=None, directory=None):
        self._owner()
        require(not self.stopped, "risk_closed")
        require(tool is None or type(tool) is CommandRiskTool, "risk_tool")
        require(tool is None or type(directory) is str and Path(directory).is_absolute(), "risk_directory")
        self.invalidate()
        self.epoch += 1
        self.tool, self.directory = tool, directory
        self.enabled = tool is not None
        self.state = "IDLE" if self.enabled else "DISABLED"
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._run, name="aitl-command-risk", daemon=True)
            self.thread.start()

    def poll(self, review, review_state, connection, session, bridge_binding):
        self._owner()
        eligible = (self.enabled and review is not None and review.direction == "TO_TERMINAL"
                    and review_state in {"REVIEW", "OVERRIDE_REQUIRED"} and connection == "CONNECTED"
                    and review.session == session
                    and (review.bridge_binding is None or review.bridge_binding == bridge_binding)
                    and (review.expires_at is None or time.time() < review.expires_at))
        if not eligible:
            self.invalidate()
            if self.enabled and (review is None or review.direction != "TO_TERMINAL"):
                self.state = "IDLE"
        else:
            if not _risk_review_valid(review):
                self.invalidate()
                self.state = "UNAVAILABLE"
                return CommandRiskSnapshot(self.state, error="risk_snapshot")
            key = CommandRiskKey(self.epoch, self.tool, review.id, review.revision, review.direction,
                                 review.session, review.bridge_binding, review.proposal_id,
                                 review.expires_at, review.final_return,
                                 hashlib.sha256(review.payload).hexdigest())
            if key != self.current:
                self.invalidate()
                self.current, self.state = key, "SCANNING"
                with self.condition:
                    self.pending = CommandRiskJob(key, review, self.directory)
                    self.condition.notify()
        for _ in range(4):
            try:
                result = self.events.get_nowait()
            except queue.Empty:
                break
            if result.key == self.current:
                self.result, self.state = result, result.state
        return CommandRiskSnapshot(self.state, self.result, self.result.error if self.result else None)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.pending is not None)
                if self.stopped:
                    return
                job, self.pending = self.pending, None
                cancel = self.cancel = threading.Event()
            result = run_command_risk(job, cancel)
            while True:
                try:
                    self.events.put_nowait(result)
                    break
                except queue.Full:
                    try:
                        self.events.get_nowait()
                    except queue.Empty:
                        pass
            with self.condition:
                if self.cancel is cancel:
                    self.cancel = None
            del job, result, cancel

    def close(self):
        self._owner()
        if self.stopped:
            return self.thread is None or not self.thread.is_alive()
        self.configure()
        with self.condition:
            self.stopped = True
            self.condition.notify()
        if self.thread:
            self.thread.join(timeout=3.0)
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        return self.thread is None or not self.thread.is_alive()


@dataclass(frozen=True)
class ConnectionProfile:
    url: str
    pin: str | None
    conversation: str
    controller_id: str
    token: str = field(repr=False)
    mode: str = "REMOTE"

    @property
    def recipient(self):
        return self.url, self.pin, self.conversation, self.controller_id


@dataclass(frozen=True)
class BridgeSnapshot:
    state: str
    mode: str | None
    target: str | None
    conversation: str | None
    instance: str | None
    generation: int
    tunnel_state: str
    last_error: str | None
    certificate_warning: bool = False
    chat_binding: dict | None = None
    chat_binding_received_at: float | None = None
    chat_binding_busy: bool = False
    chat_binding_error: str | None = None


class TransportError(ProbeError):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.retryable = retryable


def net_json(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError):
        raise TransportError("network_json") from None
    require(0 < len(raw) <= FRAME_LIMIT, "network_body_limit")
    return raw


def check_profile(profile):
    require(type(profile) is ConnectionProfile, "invalid_profile")
    parsed = urlsplit(profile.url)
    require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
            and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
            "invalid_bridge_url")
    try:
        port = parsed.port
    except ValueError:
        raise ProbeError("invalid_bridge_url") from None
    require(port is not None and 1 <= port <= 65535, "explicit_bridge_port_required")
    require(profile.mode in {"LOCAL", "REMOTE"}, "invalid_connection_mode")
    if profile.mode == "LOCAL":
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and profile.pin is None,
                "local_loopback_required")
    else:
        require(parsed.scheme == "https" and type(profile.pin) is str
                and re.fullmatch("[0-9a-f]{64}", profile.pin), "verified_pin_required")
    for value in (profile.conversation, profile.controller_id):
        require(type(value) is str and re.fullmatch("[a-zA-Z0-9_-]{16,64}", value), "invalid_binding_id")
    require(type(profile.token) is str and re.fullmatch("[0-9a-f]{64}", profile.token), "invalid_connection_token")
    return parsed


def check_certificate(der, host, pin):
    """Pinned self-signed profile, on the same TLS socket used for the request.

    CERT_NONE is confined to this explicit profile. The pin, self-signature,
    validity, SAN, key and required extensions are all verified before HTTP.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID
        cert = x509.load_der_x509_certificate(der)
        key = cert.public_key()
        spki = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        require(hmac.compare_digest(hashlib.sha256(spki).hexdigest(), pin), "tls_pin_mismatch")
        require(isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1), "tls_key_type")
        require(cert.subject == cert.issuer and isinstance(cert.signature_hash_algorithm, hashes.SHA256),
                "tls_certificate_profile")
        key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=timezone.utc)
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=timezone.utc)
        require(before <= datetime.now(timezone.utc) < after, "tls_certificate_expired")
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        extended = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        require(not constraints.ca and usage.digital_signature and not usage.key_cert_sign
                and ExtendedKeyUsageOID.SERVER_AUTH in extended, "tls_certificate_usage")
        allowed = {ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE,
                   ExtensionOID.EXTENDED_KEY_USAGE, ExtensionOID.SUBJECT_ALTERNATIVE_NAME}
        require(all(not item.critical or item.oid in allowed for item in cert.extensions), "tls_critical_extension")
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Deliberately no wildcard matching in this small private-bridge profile.
            require(host.lower() in [name.lower() for name in san.get_values_for_type(x509.DNSName)], "tls_name_mismatch")
        else:
            require(address in san.get_values_for_type(x509.IPAddress), "tls_name_mismatch")
        return after
    except ProbeError:
        raise
    except ImportError:
        raise TransportError("cryptography_package_required") from None
    except Exception:
        raise TransportError("tls_certificate_invalid") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, pin, **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port, context=context, **kwargs)
        self.pin = pin

    def connect(self):
        super().connect()
        try:
            self.valid_until = check_certificate(self.sock.getpeercert(binary_form=True), self.host, self.pin)
        except BaseException:
            self.close()
            raise


class BridgeHTTP:
    """Direct HTTP client: no proxy inheritance, URL callbacks or redirects."""
    def __init__(self, profile):
        self.profile = profile
        self.address = check_profile(profile)
        self.active = None
        self.lock = threading.Lock()
        self.certificate_warning = False

    def abort(self):
        with self.lock:
            conn = self.active
        if conn and conn.sock:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def request(self, method, path, value=None, *, authenticate=True):
        require(method in {"GET", "POST", "PUT"} and path.startswith("/v1/") and len(path) <= 2048,
                "invalid_network_action")
        conn_type = PinnedHTTPSConnection if self.address.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": 5}
        if conn_type is PinnedHTTPSConnection:
            kwargs["pin"] = self.profile.pin
        conn = conn_type(self.address.hostname, self.address.port, **kwargs)
        with self.lock:
            self.active = conn
        try:
            conn.connect()  # Pin validation happens before any header or body bytes.
            if isinstance(conn, PinnedHTTPSConnection):
                self.certificate_warning = (conn.valid_until - datetime.now(timezone.utc)).total_seconds() <= 30 * 86400
            conn.sock.settimeout(35)
            headers = {"Accept": "application/json", "Connection": "close"}
            if authenticate:
                headers["Authorization"] = "Bearer " + self.profile.token
            raw = None if value is None else value if type(value) is bytes else net_json(value)
            require(raw is None or len(raw) <= FRAME_LIMIT, "network_body_limit")
            if raw is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, raw, headers)
            response = conn.getresponse()
            require(response.getheader("Content-Encoding") in {None, "identity"}, "network_content_encoding")
            require(response.getheader("Content-Type", "").split(";")[0] == "application/json", "network_content_type")
            length = response.getheader("Content-Length")
            require(length is None or length.isdigit() and int(length) <= FRAME_LIMIT, "network_body_limit")
            body = response.read(FRAME_LIMIT + 1)
            require(0 < len(body) <= FRAME_LIMIT, "network_body_limit")
            value = decode_packet(body)
            if response.status != 200:
                if response.status == 400 and value == {"error": "invalid_cursor"} and path.startswith("/v1/inbox?"):
                    raise TransportError("network_cursor_expired", True)
                raise TransportError("network_http_" + str(response.status), response.status in {429, 502, 503, 504})
            return value
        except TransportError:
            raise
        except ProbeError as exc:
            raise TransportError(str(exc)) from None
        except (OSError, http.client.HTTPException):
            raise TransportError("network_unavailable", True) from None
        finally:
            conn.close()
            with self.lock:
                self.active = None


def validate_status(value, profile):
    fields(value, {"version", "instance", "conversation", "controller_id", "state", "retention_seconds"})
    require(type(value["version"]) is int and value["version"] == 1 and value["state"] == "PAIRED"
            and value["conversation"] == profile.conversation and value["controller_id"] == profile.controller_id
            and type(value["retention_seconds"]) is int and value["retention_seconds"] == 86400,
            "network_binding_mismatch")
    require(type(value["instance"]) is str and re.fullmatch("[0-9a-f]{32}", value["instance"]), "network_instance")
    return value["instance"]


def validate_chat_binding(value, profile, instance):
    fields(value, {"version", "instance", "conversation", "generation", "code", "issued_at",
                   "expires_at", "server_time", "status", "retry_at", "restriction_scope", "locked_principals"})
    require(type(value["version"]) is int and value["version"] == 1
            and value["instance"] == instance and value["conversation"] == profile.conversation
            and type(value["generation"]) is int and value["generation"] > 0,
            "network_binding_mismatch")
    for key in ("issued_at", "expires_at", "server_time"):
        require(type(value[key]) in {float, int} and value[key] == value[key]
                and abs(value[key]) != float("inf"), "network_schema")
    require(value["expires_at"] == value["issued_at"] + BRIDGE_CHAT_LIFETIME
            and value["issued_at"] <= value["server_time"], "network_schema")
    require(value["code"] is None and value["status"] == "EXPIRED"
            or type(value["code"]) is str and re.fullmatch("[" + BRIDGE_CHAT_ALPHABET + "]{8}", value["code"])
            and value["status"] in {"VALID", "BACKOFF", "LOCKED"}, "network_schema")
    require(value["status"] in {"VALID", "BACKOFF", "LOCKED", "EXPIRED"}
            and type(value["locked_principals"]) is int and 0 <= value["locked_principals"] <= 32,
            "network_schema")
    require(value["retry_at"] is None or type(value["retry_at"]) in {int, float}
            and value["retry_at"] == value["retry_at"] and abs(value["retry_at"]) != float("inf"),
            "network_schema")
    require(value["restriction_scope"] in {None, "BINDING", "PRINCIPAL"}, "network_schema")
    require((value["status"] in {"BACKOFF", "LOCKED"}) == (value["retry_at"] is not None)
            and (value["status"] in {"BACKOFF", "LOCKED"}) == (value["restriction_scope"] is not None),
            "network_schema")
    require(value["status"] != "EXPIRED" or value["code"] is None, "network_schema")
    return value


class ConnectionStore(PreferenceStore):
    """Separate private config. Only connection credentials, never approvals."""
    def load_connection(self):
        dfd = self._directory_fd()
        try:
            try:
                fd = os.open("connection.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "unsafe_connection_file")
                raw = source.read(16385)
            require(len(raw) <= 16384, "connection_config_limit")
            value = decode_packet(raw)
            fields(value, {"version", "profile", "tunnel"})
            require(type(value["version"]) is int and value["version"] == 1, "connection_config_version")
            if value["profile"] is not None:
                fields(value["profile"], {"url", "pin", "conversation", "controller_id", "token", "mode"})
                check_profile(ConnectionProfile(**value["profile"]))
                require(value["profile"]["mode"] == "REMOTE", "local_tokens_not_persistent")
            if value["tunnel"] is not None:
                fields(value["tunnel"], {"binary", "tunnel_id", "api_key"})
                validate_tunnel(value["tunnel"], check_binary=False)
            return value
        except OSError:
            raise ProbeError("connection_config_read_failure") from None
        finally:
            os.close(dfd)

    def save_connection(self, profile=None, tunnel=None):
        self.load_connection()  # Reject malformed, insecure or symlinked existing state.
        if profile is not None:
            check_profile(profile)
            require(profile.mode == "REMOTE", "local_tokens_not_persistent")
        if tunnel is not None:
            validate_tunnel(tunnel)
        body = {"version": 1, "profile": None if profile is None else
                {name: getattr(profile, name) for name in ("url", "pin", "conversation", "controller_id", "token", "mode")},
                "tunnel": tunnel}
        dfd = self._directory_fd()
        temporary = ".connection-" + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            with os.fdopen(fd, "wb") as target:
                target.write(net_json(body))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, "connection.json", src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
        except OSError:
            raise ProbeError("connection_config_write_failure") from None
        finally:
            try:
                os.unlink(temporary, dir_fd=dfd)
            except FileNotFoundError:
                pass
            os.close(dfd)


def isolated_service_environment():
    # Explicit allowlist: no SSH agent, proxies, Python path, API tokens or HOME.
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"}


class LocalBridgeProcess:
    """Private pipe bootstrap; transport child inherits no terminal descriptors."""
    def __init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix="aitl-bridge-"))
        os.chmod(self.directory, 0o700)
        self.process = None
        self.profile = None
        self.ai_token = secrets.token_hex(32)
        controller_token, conversation = secrets.token_hex(32), secrets.token_hex(32)
        boot_r, boot_w = os.pipe()
        ready_r, ready_w = os.pipe()
        try:
            script = Path(__file__).resolve()
            require(script.is_file(), "bridge_service_file_missing")
            self.process = subprocess.Popen([sys.executable, "-I", str(script), "--bridge",
                                             "--state-dir", str(self.directory),
                                             "--conversation", conversation, "--bootstrap-fd", str(boot_r),
                                             "--ready-fd", str(ready_w)], pass_fds=(boot_r, ready_w), close_fds=True,
                                            cwd=self.directory, env=isolated_service_environment(),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.close(boot_r)
            boot_r = None
            os.close(ready_w)
            ready_w = None
            os.write(boot_w, net_json({"controller_token": controller_token, "ai_token": self.ai_token,
                                     "conversation": conversation}))
            os.close(boot_w)
            boot_w = None
            data = bytearray()
            deadline = time.monotonic() + 5
            while not data.endswith(b"\n"):
                require(time.monotonic() < deadline and self.process.poll() is None, "bridge_start_failed")
                if select.select([ready_r], [], [], 0.05)[0]:
                    chunk = os.read(ready_r, 4096)
                    require(bool(chunk), "bridge_start_failed")
                    data.extend(chunk)
                    require(len(data) <= 4096, "bridge_ready_limit")
            value = decode_packet(bytes(data))
            fields(value, {"version", "port", "mcp_port", "instance", "conversation", "pin"})
            require(value["version"] == 1 and value["conversation"] == conversation and value["pin"] is None,
                    "bridge_ready_schema")
            for key in ("port", "mcp_port"):
                require(type(value[key]) is int and 1 <= value[key] <= 65535, "bridge_ready_port")
            provisional = ConnectionProfile("http://127.0.0.1:" + str(value["port"]), None, conversation,
                                            "0" * 32, controller_token, "LOCAL")
            status = BridgeHTTP(provisional).request("GET", "/v1/status")
            self.profile = replace(provisional, controller_id=status.get("controller_id"))
            validate_status(status, self.profile)
            binding = BridgeHTTP(self.profile).request("GET", "/v1/chat-binding")
            self.chat_code = validate_chat_binding(binding, self.profile, status["instance"])["code"]
            self.mcp_url = "http://127.0.0.1:" + str(value["mcp_port"]) + "/mcp"
        except BaseException:
            self.close()
            raise
        finally:
            for fd in (boot_r, boot_w, ready_r, ready_w):
                if fd is not None:
                    os.close(fd)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.ai_token = ""
        self.chat_code = ""
        shutil.rmtree(self.directory, ignore_errors=True)


def validate_tunnel(value, check_binary=True):
    fields(value, {"binary", "tunnel_id", "api_key"})
    require(type(value["tunnel_id"]) is str and re.fullmatch(r"tunnel_[0-9a-f]{32}", value["tunnel_id"]), "invalid_tunnel_id")
    require(type(value["api_key"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{16,512}", value["api_key"]), "invalid_tunnel_key")
    require(type(value["binary"]) is str and len(value["binary"]) <= 4096
            and Path(value["binary"]).is_absolute(), "explicit_tunnel_binary_required")
    if check_binary:
        info = Path(value["binary"]).lstat()
        require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and os.access(value["binary"], os.X_OK),
                "unsafe_tunnel_binary")


TUNNEL_RELEASE = "v0.0.15"
TUNNEL_ARCHIVE_SHA256 = {
    "amd64": "f26f8b3ee6c335e38fa5cfbe6ce5635f53738f08a26eecf07d6cebacab4a1abf",
    "arm64": "a868d295385b22449341fa141b911f3e991583e45b1fa2a5bfb946aed1861b88",
}
TUNNEL_SETTINGS_URL = "https://platform.openai.com/settings/organization/tunnels"
RUNTIME_KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"


def setup_safe_text(text, secrets=()):
    """Bounded, passive diagnostics; never publish credential/header-like lines."""
    text = str(text)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    rows = []
    for row in text.splitlines():
        if re.search(r"(?i)api[ _-]?key|authorization|bearer|password|secret|token[\s:=]|sk-[a-z0-9_-]+", row):
            rows.append("[credential-bearing line omitted]")
        else:
            row = re.sub(r"[A-Za-z0-9_-]{32,}", "[REDACTED]", row)
            row = re.sub(r"(?i)https?://[^\s/@]+:[^\s/@]+@[^\s]+", "[credential URL omitted]", row)
            rows.append("".join(char for char in row if char.isprintable() and unicodedata.category(char) != "Cf"))
    return "\n".join(rows)[:8192]


@dataclass(frozen=True)
class SetupDiagnostic:
    code: str
    phase: str
    component: str
    target: str = ""
    exit_code: int | None = None
    exception: str = ""
    stdout: str = ""
    stderr: str = ""
    note: str = ""

    def safe(self, secrets=()):
        return replace(self, **{name: setup_safe_text(getattr(self, name), secrets)
                                for name in ("code", "phase", "component", "target", "exception", "stdout", "stderr", "note")})

    def text(self):
        rows = ["AITL setup failure", "Phase: " + self.phase, "Komponente: " + self.component,
                "Grund: " + self.code, "Installationsziel: " + (self.target or "nicht verfügbar"),
                "Exit-Code: " + (str(self.exit_code) if self.exit_code is not None else "nicht verfügbar"),
                "Exception: " + (self.exception or "keine")]
        for title, value in (("Hinweis", self.note), ("stdout (gekürzt/redigiert)", self.stdout), ("stderr (gekürzt/redigiert)", self.stderr)):
            if value:
                rows.append(title + ":\n" + value)
        return "\n".join(rows)

    def cause(self):
        output = (self.stdout + "\n" + self.stderr + "\n" + self.exception).lower()
        if self.code == "client_version_unsupported":
            return "Installierter Client unterstützt den benötigten Polling-Nachweis nicht. " + self.note
        if any(word in output for word in ("permission denied", "not writable", "access denied", "eacces", "eperm")):
            return "Installer darf sein Ziel nicht beschreiben. Homebrew-Eigentümer/Provisionierung prüfen; keine automatische Rechteerhöhung."
        if "command line tools" in output or "xcode-select" in output:
            return "Homebrew meldet fehlende/fehlerhafte Xcode Command Line Tools; über den macOS-Installationsweg einrichten."
        return self.component + " scheiterte in " + self.phase + (" (Exit " + str(self.exit_code) + ")." if self.exit_code is not None else ".") + (" " + self.exception if self.exception else "")


class SetupFailure(ProbeError):
    def __init__(self, diagnostic):
        super().__init__(diagnostic.code)
        self.diagnostic = diagnostic


def setup_exception(exc, phase, component, target=""):
    if isinstance(exc, SetupFailure):
        return exc.diagnostic
    code = str(exc) if isinstance(exc, ProbeError) else "local_preparation_failed"
    detail = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        detail += " errno=" + str(exc.errno) + " " + os.strerror(exc.errno)
    return SetupDiagnostic(code, phase, component, target, exception=detail)


def setup_command(argv, environment, cancel, phase, component, target="", timeout=300):
    """Drain both streams continuously, retaining only bounded tails in memory."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {name: False for name in buffers}
    def drain(pipe, name):
        if pipe is None:
            return
        try:
            while chunk := pipe.read(4096):
                buffers[name].extend(chunk)
                if len(buffers[name]) > 16384:
                    del buffers[name][:-16384]
                    truncated[name] = True
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()
    try:
        process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   close_fds=True, start_new_session=True)
    except Exception as exc:
        raise SetupFailure(setup_exception(exc, phase, component, target)) from None
    readers = [threading.Thread(target=drain, args=(getattr(process, name), name), daemon=True)
               for name in buffers]
    for reader in readers:
        reader.start()
    error = None
    try:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            require(not cancel.wait(0.1), "local_setup_cancelled")
            require(time.monotonic() < deadline, "client_install_timeout")
    except Exception as exc:
        error = exc
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        for reader in readers:
            reader.join(timeout=2)
    captured = {}
    for name, data in buffers.items():
        text = bytes(data).decode("utf-8", errors="replace")
        if truncated[name]:
            text = "[earlier output omitted]\n" + (text.split("\n", 1)[1] if "\n" in text else "")
        captured[name] = text
    diagnostic = SetupDiagnostic("client_install_failed", phase, component, target,
                                 exit_code=process.returncode,
                                 stdout=captured["stdout"], stderr=captured["stderr"])
    if error is not None:
        diagnostic = replace(diagnostic, code=str(error) if isinstance(error, ProbeError) else "client_install_failed",
                             exception=type(error).__name__)
    if error is not None or process.returncode != 0:
        raise SetupFailure(diagnostic)
    return diagnostic


def download_tunnel_archive(url, cancel):
    import urllib.request
    class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            parsed = urlsplit(newurl)
            require(parsed.scheme == "https" and parsed.hostname in
                    {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                    and not parsed.username and not parsed.password, "client_download_redirect")
            return super().redirect_request(request, fp, code, message, headers, newurl)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirect())
    data = bytearray()
    deadline = time.monotonic() + 120
    try:
        with opener.open(url, timeout=10) as response:
            while True:
                require(not cancel.is_set(), "local_setup_cancelled")
                require(time.monotonic() < deadline, "client_download_timeout")
                chunk = response.read(65536)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
                require(len(data) <= 64 * 1024 * 1024, "client_archive_limit")
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("client_download_failed") from None


def install_tunnel_archive(archive, directory, architecture, cancel):
    import io
    import zipfile
    require(hashlib.sha256(archive).hexdigest() == TUNNEL_ARCHIVE_SHA256[architecture], "client_checksum_mismatch")
    stem = "tunnel-client-runtime-" + TUNNEL_RELEASE + "-linux-" + architecture
    allowed = {"tunnel-client-runtime", "LICENSE", "NOTICE", stem + "-licenses.txt", stem + ".spdx.json"}
    dfd = PreferenceStore(directory)._directory_fd()
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            members = source.infolist()
            require(len(members) == len(allowed) and {item.filename for item in members} == allowed,
                    "unsafe_client_archive")
            require(sum(item.file_size for item in members) <= 64 * 1024 * 1024, "client_archive_limit")
            require(all(item.file_size <= 64 * 1024 * 1024 and not item.is_dir()
                        and stat.S_IFMT(item.external_attr >> 16) in {0, stat.S_IFREG}
                        for item in members), "unsafe_client_archive")
            # Publish the executable last, after its bundled legal/evidence files.
            for item in sorted(members, key=lambda item: item.filename == "tunnel-client-runtime"):
                require(not cancel.is_set(), "local_setup_cancelled")
                temporary = ".download-" + uuid.uuid4().hex
                try:
                    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
                    with os.fdopen(fd, "wb") as target:
                        target.write(source.read(item))
                        if item.filename == "tunnel-client-runtime":
                            os.fchmod(target.fileno(), 0o700)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, item.filename, src_dir_fd=dfd, dst_dir_fd=dfd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=dfd)
                    except FileNotFoundError:
                        pass
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("unsafe_client_archive") from None
    finally:
        os.close(dfd)
    return str(Path(directory) / "tunnel-client-runtime")


def compatible_tunnel_binary(path, diagnostics=None):
    binary = str(path)
    probe_command = "tunnel-client --version"
    try:
        binary = str(Path(path).resolve(strict=True))
        validate_tunnel({"binary": binary, "tunnel_id": "tunnel_" + "0" * 32, "api_key": "0" * 16})
        result = subprocess.run([binary, "--version"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE if diagnostics is not None else subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, close_fds=True,
                                env=isolated_service_environment(), timeout=5, check=False)
        version = re.search(rb"\bv?(\d+)\.(\d+)\.(\d+)\b", result.stdout[:4096])
        if result.returncode == 0 and version and tuple(int(part) for part in version.groups()) >= (0, 0, 15):
            # Official Full/Runtime --version output need not contain the client name.
            # Probe its actual run CLI without credentials or starting a tunnel.
            probe_command = "tunnel-client run --help"
            result = subprocess.run([binary, "run", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, close_fds=True,
                                    env=isolated_service_environment(), timeout=5, check=False)
            help_text = result.stdout[:65536] + result.stderr[:65536]
            flags = set(re.findall(rb"(?m)^\s+(?:-\w,\s+)?(--[\w.-]+)(?=\s|=)", help_text))
            required = {b"--control-plane.tunnel-id", b"--control-plane.api-key", b"--mcp.server-url",
                        b"--mcp.extra-headers", b"--mcp.discovery-extra-headers",
                        b"--health.listen-addr", b"--health.url-file"}
            if result.returncode == 0 and b"Usage of run:" in help_text and required <= flags:
                return binary
        if diagnostics is not None:
            found = ".".join(part.decode("ascii") for part in version.groups()) if version else "nicht erkannt"
            identity_probe = probe_command == "tunnel-client run --help"
            diagnostics.append(SetupDiagnostic("client_version_unsupported" if result.returncode == 0 and not identity_probe else "client_probe_failed", "CLIENT_CHECK", probe_command,
                                               binary, exit_code=result.returncode,
                                               stdout=result.stdout[:4096].decode("utf-8", errors="replace"),
                                               stderr=(result.stderr or b"")[:4096].decode("utf-8", errors="replace"),
                                               note=("Client-CLI nicht erkannt: benötigte run-Optionen fehlen. " if identity_probe else "")
                                               + "Benötigt ≥0.0.15; erkannt: " + found))
    except (ProbeError, OSError, subprocess.SubprocessError) as exc:
        if diagnostics is not None:
            diagnostics.append(setup_exception(exc, "CLIENT_CHECK", probe_command, binary))
    return None


def ensure_tunnel_client(directory, previous, cancel, progress):
    candidates = [previous] if previous else []
    candidates += [shutil.which(name) for name in ("tunnel-client-runtime", "tunnel-client")]
    if sys.platform == "darwin":
        candidates += ["/opt/homebrew/bin/tunnel-client", "/usr/local/bin/tunnel-client"]
    for candidate in candidates:
        require(not cancel.is_set(), "local_setup_cancelled")
        if candidate and (binary := compatible_tunnel_binary(candidate)):
            return binary
    if sys.platform == "darwin":
        brew = shutil.which("brew") or next((path for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
                                            if Path(path).is_file()), None)
        require(brew is not None, "homebrew_required")
        progress("INSTALLING")
        environment = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": str(Path.home()),
                       "HOMEBREW_NO_ANALYTICS": "1", "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_SUDO": "1"}
        prefix_result = setup_command([brew, "--prefix"], environment, cancel, "INSTALL_TARGET", "Homebrew --prefix", timeout=10)
        prefix = prefix_result.stdout.strip()
        require(len(prefix) <= 4096 and Path(prefix).is_absolute() and "\n" not in prefix, "invalid_homebrew_prefix")
        target = str(Path(prefix) / "Cellar" / "tunnel-client")
        result = setup_command([brew, "install", "openai/tools/tunnel-client"], environment, cancel,
                               "INSTALLING", "Homebrew install openai/tools/tunnel-client", target)
        result = replace(result, note="Formula im Homebrew-Präfix " + prefix + "; keine Root-/sudo-Installation.")
        diagnostics = []
        for path in (str(Path(prefix) / "bin" / "tunnel-client"), "/opt/homebrew/bin/tunnel-client",
                     "/usr/local/bin/tunnel-client", shutil.which("tunnel-client")):
            if path and (binary := compatible_tunnel_binary(path, diagnostics)):
                return binary
        probe = next((item for item in diagnostics if item.code in {"client_version_unsupported", "client_probe_failed"}), None)
        if probe is not None:
            raise SetupFailure(replace(probe, target=target,
                                      stdout=result.stdout + "\n" + probe.stdout, stderr=result.stderr + "\n" + probe.stderr,
                                      note=probe.note + "\nHomebrew install: Exit " + str(result.exit_code) + ". " + result.note))
        raise SetupFailure(replace(result, code="client_install_failed", phase="CLIENT_CHECK",
                                  exception=diagnostics[-1].exception if diagnostics else "",
                                  note=result.note + "\nInstallation erfolgreich, aber kein kompatibler ausführbarer Client gefunden."))
    require(sys.platform.startswith("linux"), "client_platform_unsupported")
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    require(architecture in TUNNEL_ARCHIVE_SHA256, "client_platform_unsupported")
    parent = PreferenceStore(directory)._directory_fd()
    try:
        name = "tunnel-client-" + TUNNEL_RELEASE + "-linux-" + architecture
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_client_directory")
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    destination = Path(directory) / name
    binary = compatible_tunnel_binary(destination / "tunnel-client-runtime")
    if binary:
        return binary
    progress("INSTALLING")
    url = "https://github.com/openai/tunnel-client/releases/download/" + TUNNEL_RELEASE + "/" + name.replace("tunnel-client-", "tunnel-client-runtime-", 1) + ".zip"
    binary = install_tunnel_archive(download_tunnel_archive(url, cancel), destination, architecture, cancel)
    require(compatible_tunnel_binary(binary) is not None, "client_install_failed")
    return binary


def remote_bridge_command(interpreter, script, state_dir, bind, port, tls_name):
    """Build a shell-safe remote first-start command; arguments contain no credentials."""
    values = (interpreter, script, state_dir, bind, tls_name)
    require(all(type(value) is str and 0 < len(value) <= 4096
                and not any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values),
            "invalid_remote_command_value")
    require(Path(script).is_absolute() and Path(state_dir).is_absolute(), "remote_paths_must_be_absolute")
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise ProbeError("invalid_remote_bind_address") from None
    require(type(port) in {int, str} and str(port).isdigit() and 1 <= int(port) <= 65535,
            "invalid_remote_port")
    require(re.fullmatch(r"[A-Za-z0-9.-]{1,253}", tls_name) is not None or
            _is_ip_address(tls_name), "invalid_remote_tls_name")
    argv = [interpreter, script, "--bridge", "--state-dir", state_dir,
            "--bind", bind, "--port", str(port), "--tls-name", tls_name, "--pair"]
    return shlex.join(argv)


def _is_ip_address(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class TunnelProcess:
    """Launch the official client; keys use file: references, never argv/env."""
    def __init__(self, config, mcp_url, ai_token):
        validate_tunnel(config)
        parsed = urlsplit(mcp_url)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.path == "/mcp"
                and parsed.port and not parsed.query and not parsed.fragment, "local_mcp_required")
        require(re.fullmatch("[0-9a-f]{64}", ai_token), "invalid_ai_token")
        self.directory = Path(tempfile.mkdtemp(prefix="aitl-tunnel-"))
        os.chmod(self.directory, 0o700)
        self.process = None
        self.state = "STARTING"
        self.error = None
        self.health = None
        try:
            for name, content in (("api-key", config["api_key"]), ("ai-key", ai_token)):
                fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as target:
                    target.write(content)
            self.argv = [config["binary"], "run", "--control-plane.tunnel-id", config["tunnel_id"],
                         "--control-plane.api-key", "file:" + str(self.directory / "api-key"),
                         "--mcp.server-url", mcp_url,
                         "--mcp.extra-headers", "X-AITL-AI-Key: file:" + str(self.directory / "ai-key"),
                         "--mcp.discovery-extra-headers", "X-AITL-AI-Key: file:" + str(self.directory / "ai-key"),
                         "--health.listen-addr", "127.0.0.1:0", "--health.url-file", str(self.directory / "health-url"),
                         "--log.level", "error", "--log.file", "/dev/null"]
            self.process = subprocess.Popen(self.argv, cwd=self.directory, env=isolated_service_environment(),
                                            close_fds=True, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except BaseException:
            self.close()
            raise

    def refresh(self):
        if self.process.poll() is not None:
            self.state, self.error = "STOPPED", "tunnel_process_exited"
            return
        self.state = "RUNNING_NOT_READY"
        path = self.directory / "health-url"
        if not path.exists():
            return
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as source:
                info = os.fstat(source.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and not stat.S_IMODE(info.st_mode) & 0o077, "unsafe_tunnel_health_file")
                url = source.read(513).strip()
            parsed = urlsplit(url)
            require(len(url) <= 512 and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.port and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                    and not parsed.username and not parsed.password, "invalid_tunnel_health_url")
            conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=0.2)
            try:
                conn.request("GET", "/readyz", headers={"Connection": "close"})
                response = conn.getresponse()
                raw = response.read(8193)
                require(len(raw) <= 8192, "tunnel_health_limit")
                if response.status == 200:
                    self.state = "READY"
            finally:
                conn.close()
        except (ProbeError, OSError, http.client.HTTPException, ValueError):
            self.error = "tunnel_health_unavailable"

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.state = "STOPPED"
        shutil.rmtree(self.directory, ignore_errors=True)

    def polling_check(self):
        """Observe the client's own authenticated poll; do not consume work with another poll."""
        path = self.directory / "health-url"
        if not path.exists():
            return False, None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and not stat.S_IMODE(info.st_mode) & 0o077, "unsafe_tunnel_health_file")
            url = source.read(513).strip()
        parsed = urlsplit(url)
        require(len(url) <= 512 and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.port and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                and not parsed.username and not parsed.password, "invalid_tunnel_health_url")
        conn = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=0.2)
        try:
            conn.request("GET", "/health/control-plane", headers={"Connection": "close"})
            response = conn.getresponse()
            if response.status == 404:
                raise ProbeError("client_health_unsupported")
            require(response.status == 200, "client_health_unavailable")
            raw = response.read(32769)
            require(len(raw) <= 32768, "tunnel_health_limit")
            value = decode_packet(raw)
            require(type(value) is dict and type(value.get("schema_version")) is int and value.get("schema_version") == 1
                    and value.get("component") == "control-plane" and type(value.get("details")) is dict,
                    "client_health_unsupported")
            details = value["details"]
            status = details.get("http_status")
            if status in {401, 403, 404}:
                raise ProbeError({401: "runtime_key_rejected", 403: "runtime_permission_denied", 404: "runtime_tunnel_not_found"}[status])
            success = details.get("last_success")
            valid_success = False
            if isinstance(success, str) and len(success) <= 64:
                try:
                    observed = datetime.fromisoformat(success.replace("Z", "+00:00"))
                    valid_success = observed.tzinfo is not None
                except ValueError:
                    pass
            accepted = value.get("status") == "ok" and valid_success and type(details.get("consecutive_failures")) is int and details["consecutive_failures"] == 0
            failure = "runtime_rate_limited" if status == 429 else "runtime_network_unavailable" if details.get("failure_category") else None
            return accepted, failure
        except (OSError, http.client.HTTPException):
            return False, "client_health_unavailable"
        finally:
            conn.close()


class LocalSetupPreparation:
    """Background installation/Bridge bootstrap only; it cannot mutate AppCore or approve."""
    def __init__(self, directory, previous, tunnel_id, api_key):
        self.cancel = threading.Event()
        self.handoff_lock = threading.Lock()
        self.events = queue.Queue(maxsize=8)
        self.thread = threading.Thread(target=self.run, args=(directory, previous, tunnel_id, api_key),
                                       name="aitl-local-setup", daemon=False)
        self.thread.start()

    def emit(self, kind, value):
        with self.handoff_lock:
            if kind == "prepared":
                require(not self.cancel.is_set(), "local_setup_cancelled")
            self.events.put_nowait((kind, value))

    def run(self, directory, previous, tunnel_id, api_key):
        process = None
        phase, component = "PREPARING", "Client-Suche/Installation"
        try:
            binary = ensure_tunnel_client(directory, previous, self.cancel, lambda state: self.emit("stage", state))
            require(not self.cancel.is_set(), "local_setup_cancelled")
            self.emit("stage", "STARTING")
            phase, component = "STARTING", "Local Bridge"
            require(not self.cancel.is_set(), "local_setup_cancelled")
            process = LocalBridgeProcess()
            require(not self.cancel.is_set(), "local_setup_cancelled")
            self.emit("prepared", (process, {"binary": binary, "tunnel_id": tunnel_id, "api_key": api_key}))
            process = None  # Ownership passes through the private event queue to AppCore.
        except Exception as exc:
            self.emit("error", setup_exception(exc, phase, component, str(directory)).safe((api_key,)))
        finally:
            if process is not None:
                process.close()

    def close(self):
        with self.handoff_lock:
            self.cancel.set()
        self.thread.join(timeout=1)
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "prepared":
                value[0].close()


class BridgeWorker:
    """Network worker with bounded queues. It cannot call AppCore or approve()."""
    def __init__(self, profile, preference, generation, cursor="", poll_enabled=True):
        check_profile(profile)
        self.profile, self.preference, self.generation = profile, preference, generation
        self.client = BridgeHTTP(profile)
        self.events, self.commands = queue.Queue(maxsize=16), queue.Queue(maxsize=16)
        self.stop = threading.Event()
        self.cursor = cursor
        self.poll_enabled = poll_enabled
        self.thread = threading.Thread(target=self.run, name="aitl-transport", daemon=True)
        self.thread.start()

    def post(self, name, value=None):
        require(not self.stop.is_set(), "bridge_disconnected")
        try:
            self.commands.put_nowait((name, value))
        except queue.Full:
            raise ProbeError("network_backpressure") from None

    def emit(self, kind, value=None):
        try:
            self.events.put_nowait((self.generation, kind, value))
        except queue.Full:
            self.stop.set()

    def fetch_chat_binding(self, instance):
        value = self.client.request("GET", "/v1/chat-binding")
        return validate_chat_binding(value, self.profile, instance)

    def emit_chat_binding(self, instance):
        try:
            self.emit("chat_binding", self.fetch_chat_binding(instance))
        except (ProbeError, OSError):
            # Binding presentation is independent of the established bridge auth and
            # poll flow. On a failed refresh, hide the potentially stale credential.
            self.emit("chat_binding_unavailable")

    def run(self):
        instance, delay = None, 1
        next_binding_check = 0.0
        try:
            while not self.stop.is_set():
                try:
                    if instance is None:
                        # Idempotent confirm supports recovery after a lost confirm response.
                        status = self.client.request("POST", "/v1/pair/confirm", {"version": 1})
                        instance = validate_status(status, self.profile)
                        self.client.request("PUT", "/v1/preferences", {"version": 1, "block_size": self.preference})
                        self.emit("connected", instance)
                        self.emit("certificate_warning", self.client.certificate_warning)
                        self.emit_chat_binding(instance)
                        next_binding_check = time.monotonic() + 2
                        delay = 1
                    while not self.commands.empty():
                        name, value = self.commands.get_nowait()
                        if name == "advance":
                            self.cursor, self.poll_enabled = value, False
                        elif name == "poll":
                            self.poll_enabled = True
                        elif name == "preferences":
                            self.preference = value
                            result = self.client.request("PUT", "/v1/preferences", {"version": 1, "block_size": value})
                            fields(result, {"version", "block_size"})
                            require(type(result["version"]) is int and result["version"] == 1
                                    and result["block_size"] == value, "network_schema")
                        elif name == "rotate_chat_binding":
                            request_id, expected_generation = value
                            body = {"version": 1, "request_id": request_id,
                                    "expected_generation": expected_generation}
                            try:
                                try:
                                    result = self.client.request("POST", "/v1/chat-binding/rotate", body)
                                except TransportError as exc:
                                    if not exc.retryable:
                                        raise
                                    # Idempotent retry: a lost response must not rotate a
                                    # second time or make the previous code recoverable.
                                    result = self.client.request("POST", "/v1/chat-binding/rotate", body)
                                result = validate_chat_binding(result, self.profile, instance)
                                self.emit("chat_binding", result)
                                self.emit("chat_binding_rotated", None)
                            except ProbeError as exc:
                                reason = str(exc)
                                if reason == "network_http_409":
                                    reason = "chat_binding_generation_conflict"
                                self.emit("chat_binding_error", reason)
                        elif name == "output":
                            identifier, body = value
                            try:
                                result = self.client.request("POST", "/v1/approved-output", body)
                                fields(result, {"version", "output_id", "state"})
                                require(type(result["version"]) is int and result["version"] == 1 and result["state"] == "ACKNOWLEDGED"
                                        and re.fullmatch("[0-9a-f]{32}", result["output_id"]), "network_schema")
                                self.emit("output_ack", identifier)
                            except ProbeError:
                                self.emit("output_uncertain", identifier)
                                raise
                        else:
                            raise TransportError("network_action_unknown")
                    if self.poll_enabled:
                        result = self.client.request("GET", "/v1/inbox?" + urlencode({"cursor": self.cursor, "wait": 1}))
                        fields(result, {"version", "instance", "conversation", "cursor", "messages"})
                        require(type(result["version"]) is int and result["version"] == 1 and result["conversation"] == self.profile.conversation,
                                "network_binding_mismatch")
                        require(result["instance"] == instance, "network_instance_changed")
                        require(type(result["cursor"]) is str and re.fullmatch("[a-zA-Z0-9_-]{16,64}", result["cursor"])
                                and type(result["messages"]) is list and len(result["messages"]) <= 1, "network_schema")
                        self.cursor = result["cursor"]
                        if result["messages"]:
                            item = result["messages"][0]
                            fields(item, {"id", "text", "expires_at"})
                            require(type(item["id"]) is str and re.fullmatch("[0-9a-f]{32}", item["id"]), "network_message_id")
                            normalize_command(item["text"], True)
                            require(type(item["expires_at"]) in {float, int} and time.time() < item["expires_at"]
                                    <= time.time() + 86400 + 300, "network_message_expiry")
                            self.emit("proposal", (item, self.cursor))
                            self.poll_enabled = False
                    else:
                        # Keep authentication current without fetching a second local inbox item.
                        status = self.client.request("GET", "/v1/status")
                        require(validate_status(status, self.profile) == instance, "network_instance_changed")
                        self.stop.wait(1)
                    if time.monotonic() >= next_binding_check:
                        self.emit_chat_binding(instance)
                        next_binding_check = time.monotonic() + 2
                except (ProbeError, OSError) as exc:
                    error = str(exc) if isinstance(exc, ProbeError) else "network_unavailable"
                    if error == "network_cursor_expired":
                        self.cursor = ""  # Re-scan only transport data; local dedup still applies.
                    retryable = isinstance(exc, TransportError) and exc.retryable or error == "network_instance_changed"
                    self.emit("disconnected", (error, retryable))
                    if not retryable:
                        break
                    instance = None
                    if self.stop.wait(delay * (0.8 + secrets.randbelow(41) / 100)):
                        break
                    delay = min(30, delay * 2)
        except Exception:
            self.emit("disconnected", ("network_worker_failed", False))
        finally:
            self.client.abort()
            self.stop.set()

    def close(self):
        self.stop.set()
        self.client.abort()
        self.thread.join(timeout=1)


class AppCore:
    """Same local actions for Tkinter and tests. No server can call approve()."""
    def __init__(self, preference_store=None, connection_store=None):
        self.endpoint = None
        self.channel = None
        self.connection = "DISCONNECTED"
        self.session = None
        self.inbox = {}
        self.review = None
        self.review_state = "EMPTY"
        self.revision = 0
        self.paused = False
        self.output = TranscriptRing()
        self.offsets = {}
        self.preferences = "SMALL"
        self.final_return = True
        self.preference_store = preference_store
        if preference_store is not None:
            self.final_return, self.preferences = preference_store.load()
        self.output_mode = "LAST_20"
        self.last_transfer_offset = None
        self.approved_outputs = deque(maxlen=20)
        self.last_error = None
        self.connection_store = connection_store
        self.saved_connection = connection_store.load_connection() if connection_store else None
        self.bridge_profile = None
        self.bridge_process = None
        self.bridge_worker = None
        self.bridge_state = "DISCONNECTED"
        self.bridge_instance = None
        self.bridge_generation = 0
        self.worker_id = 0
        self.bridge_error = None
        self.certificate_warning = False
        self.chat_binding = None
        self.chat_binding_received_at = None
        self.chat_binding_busy = False
        self.chat_binding_error = None
        self.chat_binding_notice = None
        self.proposal_metadata = {}
        self.received_messages = {}
        self.bridge_cursor = ""
        self.last_proposal_id = None
        self.last_proposal_binding = None
        self.last_proposal_expiry = None
        self.transferred = []  # Stable submission records for explicit output binding.
        self.pending_output = None
        self.tunnel = None
        self.tunnel_check_at = 0
        self.pair_events = queue.Queue(maxsize=1)
        self.pair_thread = None
        self.local_setup = None
        self.local_setup_state = "IDLE"
        self.local_setup_error = None
        self.local_setup_diagnostic = None
        self.local_pending = None
        self.local_setup_deadline = 0
        self.local_check_at = 0
        self._command_risk = CommandRiskReviewer()
        self.risk_requested = False
        self.risk_setup = None
        self.risk_installation = None
        self.risk_setup_state = "DISABLED"
        self.risk_setup_error = None
        self.risk_setup_epoch = 0
        self.risk_setup_pending = None

    def request_command_risk(self, enabled):
        self._command_risk._owner()
        require(type(enabled) is bool, "risk_enable")
        self.risk_setup_epoch += 1
        self.risk_requested = enabled
        self._command_risk.configure()
        if self.risk_setup:
            self.risk_setup.close()
        self.risk_setup_pending = None
        self.risk_setup_state, self.risk_setup_error = "DISABLED", None
        if not enabled:
            return
        self.risk_installation = None
        try:
            release = command_risk_release()
            release.validate()  # BEFORE any filesystem, dialog, transport or execution.
            base = self.preference_store.directory if self.preference_store else preference_directory()
            self.risk_installation = CommandRiskInstallation(base, release)
            self._queue_command_risk_setup(False)
        except ProbeError as exc:
            self.risk_setup_state, self.risk_setup_error = "UNAVAILABLE", str(exc)

    def confirm_command_risk_install(self, confirmed):
        self._command_risk._owner()
        require(self.risk_requested and self.risk_setup_state == "INSTALL_CONFIRM", "risk_install_state")
        if not confirmed:
            self.request_command_risk(False)
            return
        self.risk_installation.release.validate()
        self._queue_command_risk_setup(True)

    def _queue_command_risk_setup(self, install):
        self.risk_setup_pending = (self.risk_installation, install)
        self.risk_setup_state = "WAITING_FOR_REAP"
        self._pump_command_risk_setup()

    def remove_command_risk(self):
        self._command_risk._owner()
        require(self.risk_installation is not None and not self.risk_requested
                and (self.risk_setup is None or not self.risk_setup.thread.is_alive())
                and self._command_risk.quiescent(), "risk_cleanup_busy")
        self.risk_installation.remove()

    def _pump_command_risk_setup(self):
        setup = self.risk_setup
        if setup is not None:
            try:
                kind, value = setup.events.get_nowait()
            except queue.Empty:
                kind, value = None, None
            if kind is not None:
                self.risk_setup = None
                if self.risk_requested and not setup.cancel.is_set():
                    if kind == "ready":
                        self._command_risk.configure(*value)
                        self.risk_setup_state, self.risk_setup_error = "READY", None
                    elif kind == "missing":
                        self.risk_setup_state = "INSTALL_CONFIRM"
                    else:
                        self.risk_setup_state, self.risk_setup_error = "UNAVAILABLE", value
            # Even a completion event is not permission to overlap the old worker.
            if setup.thread.is_alive():
                self.risk_setup = setup
        if (self.risk_requested and self.risk_setup_pending is not None
                and (self.risk_setup is None or not self.risk_setup.thread.is_alive())
                and self._command_risk.quiescent()):
            installation, install = self.risk_setup_pending
            self.risk_setup_pending = None
            self.risk_setup = CommandRiskSetup(installation, install=install)
            self.risk_setup_state = "INSTALLING" if install else "CHECKING_TOOL"

    def set_command_risk(self, tool=None, directory=None):
        """Local headless setup only; callers own artifact admission. Default OFF."""
        self._command_risk.configure(tool, directory)
        return self.command_risk_snapshot()

    def command_risk_snapshot(self):
        self._command_risk._owner()
        self._pump_command_risk_setup()
        if self.risk_requested and self.risk_setup_state != "READY":
            return CommandRiskSnapshot(self.risk_setup_state, error=self.risk_setup_error)
        return self._command_risk.poll(self.review, self.review_state, self.connection,
                                       self.session, self._bridge_binding())

    def snapshot(self):
        return AppSnapshot(self.connection, self.session, tuple(self.inbox.items()),
                           self.review, self.review_state, self.paused, self.output.start,
                           self.output.end, self.preferences, tuple(self.approved_outputs), self.last_error,
                           self.final_return, self.output_mode, self.last_transfer_offset,
                           self.bridge_snapshot(), self.command_risk_snapshot())

    def bridge_snapshot(self):
        profile = self.bridge_profile
        return BridgeSnapshot(self.bridge_state, profile.mode if profile else None,
                              profile.url if profile else None, profile.conversation if profile else None,
                              self.bridge_instance, self.bridge_generation,
                              self.tunnel.state if self.tunnel else "STOPPED", self.bridge_error,
                              self.certificate_warning, dict(self.chat_binding) if self.chat_binding else None,
                              self.chat_binding_received_at, self.chat_binding_busy, self.chat_binding_error)

    def _bridge_binding(self):
        if self.bridge_state != "CONNECTED" or self.bridge_profile is None:
            return None
        return self.bridge_profile.recipient + (self.bridge_instance, self.bridge_generation)

    def _invalidate_bridge_review(self):
        if self.review and self.review.bridge_binding:
            self._command_risk.invalidate()
            if self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}:
                self.review_state = "STALE"
            elif self.review_state == "SENDING":
                self.review_state = "DELIVERY_UNCERTAIN"

    def connect_profile(self, profile):
        check_profile(profile)
        same_recipient = self.bridge_profile is not None and self.bridge_profile.recipient == profile.recipient
        self.disconnect_bridge()
        if not same_recipient:
            self.inbox.clear()  # No proposal may cross a changed conversation or target.
            self.proposal_metadata.clear()
            self.received_messages.clear()
            self.bridge_cursor = ""
            self.last_proposal_id = None
            self.last_proposal_binding = None
            self.transferred.clear()
        self.bridge_profile = profile
        self.bridge_state = "CONNECTING"
        self.bridge_error = None
        self.certificate_warning = False
        self.worker_id += 1
        self.bridge_worker = BridgeWorker(profile, self.preferences, self.worker_id,
                                          self.bridge_cursor, not self.inbox)

    def reconnect_bridge(self):
        require(self.bridge_profile is not None, "no_previous_connection")
        if self.bridge_profile.mode == "LOCAL":
            require(self.bridge_process is not None and self.bridge_process.process.poll() is None,
                    "local_bridge_not_running")
        self._invalidate_bridge_review()
        if self.bridge_worker:
            self.bridge_worker.close()
        self.bridge_generation += 1
        self.worker_id += 1
        self.bridge_instance = None
        self.chat_binding = None
        self.chat_binding_received_at = None
        self.chat_binding_busy = False
        self.chat_binding_error = None
        self.bridge_state, self.bridge_error = "CONNECTING", None
        self.bridge_worker = BridgeWorker(self.bridge_profile, self.preferences, self.worker_id,
                                          self.bridge_cursor, not self.inbox)

    def connect_local_bridge(self):
        self.disconnect_bridge()
        process = LocalBridgeProcess()
        try:
            self.connect_profile(process.profile)
        except BaseException:
            process.close()
            raise
        self.bridge_process = process

    def connect_saved_bridge(self):
        if self.connection_store is not None:
            # A claim may have been saved before its confirm reply was lost.
            self.saved_connection = self.connection_store.load_connection()
        require(self.saved_connection is not None and self.saved_connection["profile"] is not None,
                "no_saved_remote_connection")
        self.connect_profile(ConnectionProfile(**self.saved_connection["profile"]))

    def disconnect_bridge(self):
        if self.local_setup is not None:
            self.local_setup.close()
            self.local_setup = None
        self.local_pending = None
        self.local_setup_state = "IDLE"
        self._invalidate_bridge_review()
        self.bridge_generation += 1
        self.bridge_state = "DISCONNECTED"
        self.bridge_instance = None
        self.chat_binding = None
        self.chat_binding_received_at = None
        self.chat_binding_busy = False
        self.chat_binding_error = None
        self.chat_binding_notice = None
        self.worker_id += 1
        if self.bridge_worker:
            self.bridge_worker.close()
            self.bridge_worker = None
        self.stop_tunnel()
        if self.bridge_process:
            self.bridge_process.close()
            self.bridge_process = None

    def rotate_chat_binding(self):
        require(self.bridge_state == "CONNECTED" and self.bridge_worker is not None, "bridge_disconnected")
        require(self.chat_binding is not None and not self.chat_binding_busy, "chat_binding_unavailable")
        self.bridge_worker.post("rotate_chat_binding", (uuid.uuid4().hex, self.chat_binding["generation"]))
        self.chat_binding_busy = True
        self.chat_binding_error = None
        self.chat_binding_notice = None

    def pair_remote(self, url, pin, code):
        """Synchronous headless setup action; no terminal or review side effects."""
        require(self.connection_store is not None, "connection_store_required")
        normalized_pin = re.sub(r"[\s:-]", "", bounded_string(pin, 128)).lower()
        provisional = ConnectionProfile(url.rstrip("/"), normalized_pin, "0" * 32, "0" * 32, "0" * 64)
        client = BridgeHTTP(provisional)
        claim = client.request("POST", "/v1/pair/claim",
                               {"code": bounded_string(code, 16), "request_id": uuid.uuid4().hex}, authenticate=False)
        fields(claim, {"version", "token", "controller_id", "conversation", "instance", "state"})
        require(type(claim["version"]) is int and claim["version"] == 1 and claim["state"] == "PAIR_PENDING", "pair_schema")
        profile = replace(provisional, conversation=claim["conversation"], controller_id=claim["controller_id"], token=claim["token"])
        check_profile(profile)
        saved = self.connection_store.load_connection()
        self.connection_store.save_connection(profile, saved["tunnel"] if saved else None)
        # Save before confirm. On lost confirm response, connect_saved_bridge recovers
        # idempotently with this exact already-pinned credential, no code reuse.
        validate_status(BridgeHTTP(profile).request("POST", "/v1/pair/confirm", {"version": 1}), profile)
        return profile

    def begin_pair_remote(self, url, pin, code):
        require(self.pair_thread is None or not self.pair_thread.is_alive(), "pair_already_running")
        self.disconnect_bridge()
        self.bridge_state = "PAIRING"
        def run():
            try:
                result = self.pair_remote(url, pin, code)
                self.pair_events.put_nowait(("paired", result))
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ProbeError) else "pair_failed"
                self.pair_events.put_nowait(("pair_error", reason))
        self.pair_thread = threading.Thread(target=run, name="aitl-pairing", daemon=True)
        self.pair_thread.start()

    def configure_tunnel(self, binary, tunnel_id, api_key):
        require(self.connection_store is not None, "connection_store_required")
        config = {"binary": binary, "tunnel_id": tunnel_id, "api_key": api_key}
        saved = self.connection_store.load_connection()
        profile = ConnectionProfile(**saved["profile"]) if saved and saved["profile"] else None
        self.connection_store.save_connection(profile, config)
        self.saved_connection = self.connection_store.load_connection()

    def begin_local_setup(self, tunnel_id, api_key):
        require(self.connection_store is not None, "connection_store_required")
        require(self.local_setup is None and self.local_pending is None
                and not (self.pair_thread is not None and self.pair_thread.is_alive()), "setup_busy")
        self.local_setup_error = None
        self.local_setup_diagnostic = None
        validate_tunnel({"binary": "/pending-client", "tunnel_id": tunnel_id, "api_key": api_key}, check_binary=False)
        previous = self.saved_connection["tunnel"]["binary"] if self.saved_connection and self.saved_connection["tunnel"] else None
        self.disconnect_bridge()
        self.local_setup_error = None
        self.local_setup_state = "PREPARING"
        self.local_setup = LocalSetupPreparation(self.connection_store.directory, previous, tunnel_id, api_key)

    def _fail_local_setup(self, reason, diagnostic=None):
        secrets = [self.local_pending["api_key"]] if self.local_pending else []
        if self.saved_connection:
            if self.saved_connection["tunnel"]:
                secrets.append(self.saved_connection["tunnel"]["api_key"])
            if self.saved_connection["profile"]:
                secrets.append(self.saved_connection["profile"]["token"])
        if self.bridge_profile:
            secrets.extend((self.bridge_profile.token, self.bridge_profile.conversation))
        if self.bridge_process:
            secrets.append(self.bridge_process.ai_token)
        diagnostic = diagnostic or SetupDiagnostic(reason, self.local_setup_state, "Local Setup")
        diagnostic = diagnostic.safe(secrets)
        self.disconnect_bridge()
        self.local_setup_state, self.local_setup_error = "FAILED", diagnostic.code
        self.local_setup_diagnostic = diagnostic
        print(diagnostic.text(), file=sys.stderr, flush=True)

    def _pump_local_setup(self):
        preparation = self.local_setup
        if preparation is not None:
            while not preparation.events.empty():
                kind, value = preparation.events.get_nowait()
                if kind == "stage":
                    self.local_setup_state = value
                elif kind == "error":
                    if isinstance(value, SetupDiagnostic):
                        self._fail_local_setup(value.code, value)
                    else:
                        self._fail_local_setup(value)
                    return
                elif kind == "prepared":
                    process, config = value
                    self.local_setup = None
                    preparation.close()
                    try:
                        self.connect_profile(process.profile)
                    except Exception as exc:
                        process.close()
                        self._fail_local_setup("bridge_start_failed", setup_exception(exc, "CONNECTING", "Local Bridge"))
                        return
                    self.bridge_process = process
                    self.local_pending = config
                    self.local_setup_state = "CONNECTING"
                    self.local_setup_deadline = time.monotonic() + 45
        if self.local_pending is None:
            return
        if self.bridge_state == "BLOCKED":
            self._fail_local_setup(self.bridge_error or "bridge_start_failed")
            return
        if time.monotonic() > self.local_setup_deadline:
            self._fail_local_setup(self.local_setup_error or "local_setup_timeout")
            return
        if self.bridge_state != "CONNECTED":
            return
        if self.tunnel is None:
            try:
                self.tunnel = TunnelProcess(self.local_pending, self.bridge_process.mcp_url, self.bridge_process.ai_token)
                self.local_setup_state = "VALIDATING"
            except Exception as exc:
                self._fail_local_setup("tunnel_process_exited", replace(setup_exception(exc, "CLIENT_START", "Tunnel-Client"), code="tunnel_process_exited"))
            return
        if self.tunnel.state == "STOPPED":
            self._fail_local_setup("tunnel_process_exited", SetupDiagnostic("tunnel_process_exited", "CLIENT_START", "Tunnel-Client",
                                                                           exit_code=self.tunnel.process.poll()))
            return
        if time.monotonic() < self.local_check_at:
            return
        self.local_check_at = time.monotonic() + 0.5
        try:
            polling, failure = self.tunnel.polling_check()
            if failure:
                self.local_setup_error = failure
            if self.tunnel.state == "READY" and polling:
                saved = self.connection_store.load_connection()
                remote = ConnectionProfile(**saved["profile"]) if saved and saved["profile"] else None
                self.connection_store.save_connection(remote, self.local_pending)
                self.saved_connection = self.connection_store.load_connection()
                self.local_pending = None
                self.local_setup_state, self.local_setup_error = "CONNECTED", None
        except ProbeError as exc:
            self._fail_local_setup(str(exc), setup_exception(exc, "VALIDATING", "Client-Polling/Konfiguration"))
        except Exception as exc:
            self._fail_local_setup("local_setup_failed", replace(setup_exception(exc, "VALIDATING", "Client-Polling/Konfiguration"), code="local_setup_failed"))

    def start_tunnel(self):
        require(self.bridge_state == "CONNECTED" and self.bridge_process is not None, "local_bridge_required")
        require(self.saved_connection is not None and self.saved_connection["tunnel"] is not None, "tunnel_config_required")
        require(self.tunnel is None, "tunnel_already_running")
        self.tunnel = TunnelProcess(self.saved_connection["tunnel"], self.bridge_process.mcp_url, self.bridge_process.ai_token)

    def stop_tunnel(self):
        if self.tunnel:
            self.tunnel.close()
            self.tunnel = None

    def retry_output(self, identifier):
        require(self.review is not None and self.review.id == identifier
                and self.review_state == "DELIVERY_UNCERTAIN", "no_uncertain_output")
        require(not self.paused and self.bridge_state == "CONNECTED" and self.pending_output is not None,
                "bridge_disconnected")
        pending = self.pending_output
        require(pending["recipient"] == self.bridge_profile.recipient and time.time() < pending["expires"],
                "output_retry_binding")
        # No new selection and no payload reconstruction during retry.
        self.bridge_worker.post("output", (identifier, pending["body"]))
        self.review_state = "SENDING"

    def _pump_bridge(self):
        if not self.pair_events.empty():
            kind, value = self.pair_events.get_nowait()
            if kind == "paired" and self.bridge_state == "PAIRING":
                self.saved_connection = self.connection_store.load_connection()
                self.connect_profile(value)
            elif kind == "pair_error" and self.bridge_state == "PAIRING":
                self.bridge_state, self.bridge_error = "BLOCKED", value
        worker = self.bridge_worker
        if worker:
            for _ in range(16):
                try:
                    identifier, kind, value = worker.events.get_nowait()
                except queue.Empty:
                    break
                if identifier != self.worker_id:
                    continue
                if kind == "connected":
                    self._invalidate_bridge_review()
                    self.bridge_generation += 1
                    self.bridge_instance = value
                    self.bridge_state, self.bridge_error = "CONNECTED", None
                    # A queued item remains locally unreviewed; new review binds anew.
                elif kind == "disconnected":
                    self._invalidate_bridge_review()
                    self.bridge_generation += 1
                    self.bridge_instance = None
                    self.chat_binding = None
                    self.chat_binding_received_at = None
                    self.chat_binding_busy = False
                    self.chat_binding_error = None
                    error, retryable = value
                    self.bridge_state = "RETRYING" if retryable else "BLOCKED"
                    self.bridge_error = error
                elif kind == "proposal":
                    item, cursor = value
                    require(self.bridge_state == "CONNECTED", "bridge_event_order")
                    self.received_messages = {key: old for key, old in self.received_messages.items() if old[1] > time.time()}
                    previous = self.received_messages.get(item["id"])
                    if previous:
                        require(previous == (item["text"], item["expires_at"]), "network_message_changed")
                        self.bridge_cursor = cursor
                        worker.post("advance", cursor)
                        if not self.inbox:
                            worker.post("poll")
                        continue
                    require(not self.inbox and len(self.received_messages) < 1024, "network_inbox_conflict")
                    self.inbox[item["id"]] = item["text"]
                    self.proposal_metadata[item["id"]] = (self.bridge_profile.recipient, item["expires_at"])
                    self.received_messages[item["id"]] = (item["text"], item["expires_at"])
                    self.bridge_cursor = cursor
                    worker.post("advance", cursor)
                elif kind == "output_ack":
                    require(self.review is not None and value == self.review.id and self.pending_output is not None,
                            "network_output_binding")
                    require(self.bridge_profile.recipient == self.pending_output["recipient"], "network_output_binding")
                    self.review_state = "ACKNOWLEDGED"
                    self.approved_outputs.append(self.review)
                    self.pending_output = None
                elif kind == "output_uncertain":
                    if self.review and self.review.id == value:
                        self.review_state = "DELIVERY_UNCERTAIN"
                elif kind == "certificate_warning":
                    require(type(value) is bool, "network_schema")
                    self.certificate_warning = value
                elif kind == "chat_binding":
                    require(type(value) is dict and value["instance"] == self.bridge_instance
                            and value["conversation"] == self.bridge_profile.conversation,
                            "network_binding_mismatch")
                    self.chat_binding = value
                    self.chat_binding_received_at = time.monotonic()
                    self.chat_binding_busy = False
                    self.chat_binding_error = None
                    self.chat_binding_notice = None
                elif kind == "chat_binding_unavailable":
                    self.chat_binding = None
                    self.chat_binding_received_at = None
                    self.chat_binding_busy = False
                    self.chat_binding_error = "chat_binding_unavailable"
                elif kind == "chat_binding_error":
                    self.chat_binding = None
                    self.chat_binding_received_at = None
                    self.chat_binding_busy = False
                    self.chat_binding_error = value
                elif kind == "chat_binding_rotated":
                    self.chat_binding_notice = "Chat-Code neu erzeugt · alten Code nicht mehr verwenden"
                else:
                    raise ProbeError("network_event_unknown")
            if worker.stop.is_set() and self.bridge_state in {"CONNECTED", "CONNECTING", "RETRYING"}:
                self._invalidate_bridge_review()
                self.bridge_generation += 1
                self.chat_binding = None
                self.chat_binding_received_at = None
                self.chat_binding_busy = False
                self.bridge_state, self.bridge_error = "BLOCKED", "network_worker_stopped"
        if self.tunnel and time.monotonic() >= self.tunnel_check_at:
            self.tunnel_check_at = time.monotonic() + 2
            self.tunnel.refresh()

    def start_local(self, workdir):
        require(self.endpoint is None, "session_already_started")
        self.endpoint = LocalEndpoint(workdir)
        self.last_error = None
        self.connection = "WAITING_FOR_TERMINAL"
        return str(self.endpoint.directory)

    def restart_local(self):
        require(self.connection == "DISCONNECTED", "terminal_not_disconnected")
        workdir = self.endpoint.workdir if self.endpoint else os.getcwd()
        if self.endpoint:
            self.endpoint.close()
            self.endpoint = None
        self.output = TranscriptRing()
        self.offsets.clear()
        self.last_transfer_offset = None
        self.output_mode = "LAST_20"
        self.last_error = None
        return self.start_local(workdir)

    def helper_argv(self):
        require(self.endpoint is not None, "no_endpoint")
        return ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "TERM=xterm-256color",
                sys.executable, "-I", str(Path(__file__).resolve()),
                "--terminal-helper", str(self.endpoint.directory)]

    def helper_command(self):
        return shlex.join(self.helper_argv())

    def launch_terminal(self):
        require(self.connection == "WAITING_FOR_TERMINAL", "terminal_already_attached")
        if sys.platform == "darwin":
            script = 'on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run'
            return subprocess.Popen(["/usr/bin/osascript", "-e", script, self.helper_command()],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True)
        terminal = shutil.which("xterm")
        require(bool(terminal), "xterm_missing_use_manual_command")
        resources = []
        for name in ("allowWindowOps", "allowFontOps", "allowTitleOps", "allowColorOps", "allowTcapOps", "allowSendEvents"):
            resources.extend(["-xrm", "XTerm*" + name + ": false"])
        return subprocess.Popen([terminal, "-T", "Admin in the Loop — Local terminal"] + resources + ["-e"] + self.helper_argv(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)

    def add_proposal(self, text):
        # Local offline import only; no files or URLs are opened.
        bounded_string(text, MAX_COMMAND)
        require(len(text.encode("utf-8")) <= MAX_COMMAND, "command_too_large")
        require(not self.inbox, "proposal_slot_occupied")
        require(self.bridge_state == "DISCONNECTED", "offline_import_requires_disconnection")
        identifier = uuid.uuid4().hex
        self.inbox[identifier] = text
        return identifier

    def remove_proposal(self, identifier):
        require(identifier in self.inbox, "unknown_proposal")
        del self.inbox[identifier]
        self.proposal_metadata.pop(identifier, None)
        if self.bridge_worker:
            self.bridge_worker.post("poll")

    def _new_review(self, direction, text, payload, final_return=False, lost_prefix=False):
        require(self.review_state not in {"CHECKING", "TRANSFERRING", "SENDING", "DELIVERY_UNCERTAIN"}, "transfer_in_progress")
        require(self.session is not None and self.connection == "CONNECTED", "terminal_not_connected")
        self._command_risk.invalidate()
        self.revision += 1
        self.review = LocalReview(uuid.uuid4().hex, self.revision, direction, self.session,
                                  text, payload, final_return, lost_prefix, self._bridge_binding())
        self.review_state = "REVIEW"
        return self.review

    def review_proposal(self, identifier, final_return=None):
        require(identifier in self.inbox, "unknown_proposal")
        metadata = self.proposal_metadata.get(identifier)
        if metadata:
            require(self.bridge_state == "CONNECTED" and metadata[0] == self.bridge_profile.recipient
                    and metadata[1] > time.time(), "proposal_binding_expired")
        review = self.review_command(self.inbox[identifier], final_return)
        if metadata:
            self.review = review = replace(review, proposal_id=identifier, expires_at=metadata[1])
        del self.inbox[identifier]  # Consume only after successful review construction.
        self.proposal_metadata.pop(identifier, None)
        if self.bridge_worker:
            self.bridge_worker.post("poll")
        return review

    def review_pending_proposal(self):
        require(bool(self.inbox), "no_pending_proposal")
        return self.review_proposal(next(iter(self.inbox)))

    def review_command(self, text, final_return=None):
        if final_return is None:
            final_return = self.final_return
        payload = normalize_command(text, final_return)
        return self._new_review("TO_TERMINAL", payload.decode("utf-8"), payload, final_return)

    def edit_review(self, identifier, text, final_return=None):
        self._active(identifier)
        require(self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}, "review_not_editable")
        if self.review.direction == "TO_TERMINAL":
            previous = self.review
            self.review_command(text, final_return)
            self.review = replace(self.review, proposal_id=previous.proposal_id, expires_at=previous.expires_at,
                                  edited=previous.edited or self.review.text.rstrip("\n") != previous.text.rstrip("\n"))
            return self.review
        require(final_return is None or type(final_return) is bool and not final_return,
                "output_return_forbidden")
        previous = self.review
        self._output_review(text, previous.lost_prefix)
        self.review = replace(self.review, edited=previous.edited or text != previous.text)
        return self.review

    def select_output(self, lines=50, since=None):
        require(lines is None and type(since) is int or type(lines) is int and 1 <= lines <= 500,
                "line_limit")
        raw, lost_prefix = self.output.selected_text(since)
        # Select lines from the already streamed passive projection, not raw escape data.
        if lines is not None:
            rows = raw.split(b"\n")
            terminated = raw.endswith(b"\n")
            if terminated:
                rows.pop()  # An LF terminates the preceding line, not a new empty row.
            raw = b"\n".join(rows[-lines:]) + (b"\n" if terminated else b"")
        require(len(raw) <= OUTPUT_LIMIT, "output_selection_limit")
        return self._output_review(raw.decode("utf-8"), lost_prefix)

    def set_output_mode(self, mode):
        require(type(mode) is str and mode in OUTPUT_MODES, "invalid_output_mode")
        require(mode != "SINCE_LAST" or self.last_transfer_offset is not None, "no_command_transferred")
        self.output_mode = mode

    def select_current_output(self):
        if self.output_mode == "SINCE_LAST":
            require(self.last_transfer_offset is not None, "no_command_transferred")
            require(self.last_transfer_offset <= self.output.end, "output_not_yet_observed")
            return self.select_output(None, since=self.last_transfer_offset)
        return self.select_output(int(self.output_mode.split("_")[1]))

    def _output_review(self, text, lost_prefix=False):
        bounded_string(text, OUTPUT_LIMIT)
        payload = text.encode("utf-8")
        require(0 < len(payload) <= OUTPUT_LIMIT, "output_selection_limit")
        require(len(net_json({"text": text})) <= FRAME_LIMIT - 2048, "output_envelope_limit")
        # No active or invisible text; LF stays as text, never terminal input.
        require(all(c == "\n" or c == " " or not unicodedata.category(c).startswith(("C", "Z"))
                    for c in text), "output_invisible_character")
        return self._new_review("TO_OUTPUT_SINK", text, payload, lost_prefix=lost_prefix)

    def _active(self, identifier):
        require(self.review is not None and self.review.id == identifier, "stale_review")
        require(self.review.session == self.session and self.connection == "CONNECTED", "stale_session")
        if self.review.bridge_binding:
            require(self.review.bridge_binding == self._bridge_binding(), "stale_bridge_binding")
        if self.review.expires_at is not None:
            require(time.time() < self.review.expires_at, "review_expired")

    def output_candidates(self):
        """Unexpired, explicitly transferred submissions for the current bridge; one list."""
        if self.bridge_profile is None or self.bridge_state != "CONNECTED":
            return ()
        now = time.time()
        return tuple({"submission_id": item["submission_id"], "expires_at": item["expires"],
                      "text_view": item["text_preview"]}
                     for item in list(self.transferred)
                     if item["binding"] == self.bridge_profile.recipient
                     and item["expires"] is not None and now < item["expires"])

    def output_binding_record(self, submission_id):
        if self.bridge_profile is None or self.bridge_state != "CONNECTED":
            return None
        for item in self.transferred:
            if item["submission_id"] == submission_id and item["binding"] == self.bridge_profile.recipient:
                if item["expires"] is None or time.time() < item["expires"]:
                    return item
        return None

    def _record_transferred_submission(self, review):
        self.transferred = [item for item in self.transferred
                            if item["submission_id"] != review.proposal_id]
        self.transferred.append({"submission_id": review.proposal_id,
                                 "binding": self.review.bridge_binding[:4],
                                 "expires": review.expires_at,
                                 "text_preview": review.text[:64]})

    def approve(self, identifier, override=False, submission_id=None, explicit_binding=False):
        self._active(identifier)
        require(type(override) is bool, "override_type")
        require(type(explicit_binding) is bool, "invalid_binding_mode")
        require(not self.paused, "paused")
        require(self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}, "approval_already_consumed")
        require(not override or self.review_state == "OVERRIDE_REQUIRED", "override_not_requested")
        if self.review.direction == "TO_OUTPUT_SINK":
            require(not override, "override_not_requested")
            if self.bridge_profile is not None:
                require(self.bridge_state == "CONNECTED" and self.review.bridge_binding == self._bridge_binding(),
                        "bridge_disconnected")
                if explicit_binding:
                    # The human chose from the Review slot; an empty choice stays genuinely unbound.
                    proposal = None
                    if submission_id is not None:
                        require(type(submission_id) is str and self.output_binding_record(submission_id) is not None,
                                "unknown_output_binding")
                        proposal = submission_id
                elif submission_id is not None:
                    require(type(submission_id) is str and self.output_binding_record(submission_id) is not None,
                            "unknown_output_binding")
                    proposal = submission_id
                else:
                    proposal = self.last_proposal_id if self.last_proposal_binding == self.bridge_profile.recipient \
                        and self.last_proposal_expiry and time.time() < self.last_proposal_expiry else None
                approved_at = time.time()
                body = net_json({"version": 1, "conversation": self.bridge_profile.conversation,
                                 "idempotency_key": self.review.id, "text": self.review.text,
                                 "submission_id": proposal, "approved_at": approved_at})
                self.bridge_worker.post("output", (identifier, body))
                self.pending_output = {"recipient": self.bridge_profile.recipient, "body": body,
                                       "expires": approved_at + 86400}
                self.review_state = "SENDING"
                return
            self.approved_outputs.append(self.review)
            self.review_state = "ACKNOWLEDGED_LOCAL"
            return
        self.review_state = "CHECKING"
        try:
            self.channel.send({"type": "transfer", "id": self.review.id,
                               "session": self.review.session, "text": self.review.text,
                               "return": self.review.final_return, "override": override})
        except (ProbeError, OSError):
            self.review_state = "TRANSFER_UNCERTAIN"
            raise ProbeError("transfer_uncertain") from None

    def verify_draft(self, identifier, text, final_return=False):
        self._active(identifier)
        if self.review.direction == "TO_TERMINAL":
            require(normalize_command(text, final_return) == self.review.payload, "unapplied_draft")
        else:
            require(type(final_return) is bool and not final_return and text == self.review.text, "unapplied_draft")

    def reject(self, identifier):
        # An uncertain output may be discarded locally without a delivery retry.
        if self.review and self.review.id == identifier and self.review_state == "DELIVERY_UNCERTAIN":
            self.pending_output = None
            self.review_state = "REJECTED"
            return
        self._active(identifier)
        require(self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}, "review_not_active")
        self.review_state = "REJECTED"

    def set_paused(self, value):
        require(type(value) is bool, "invalid_pause")
        self.paused = value

    def set_preferences(self, value):
        require(value in BLOCK_SIZES, "invalid_preference")
        if self.preference_store:
            self.preference_store.save(self.final_return, value)
        self.preferences = value
        if self.bridge_worker and self.bridge_state == "CONNECTED":
            self.bridge_worker.post("preferences", value)

    def set_final_return(self, value):
        require(type(value) is bool, "invalid_return_preference")
        require(self.review_state not in {"CHECKING", "TRANSFERRING"}, "transfer_in_progress")
        if self.preference_store:
            self.preference_store.save(value, self.preferences)
        self.final_return = value
        if self.review and self.review.direction == "TO_TERMINAL" and self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}:
            return self.edit_review(self.review.id, self.review.text, value)

    def pump(self):
        try:
            self._pump_local_setup()
            self._pump_bridge()
        except (ProbeError, OSError) as exc:
            self.bridge_error = str(exc) if isinstance(exc, ProbeError) else "network_failure"
            self.disconnect_bridge()
            self.bridge_state = "BLOCKED"
        try:
            if not self.channel and self.endpoint and not self.endpoint.bound:
                self.channel = self.endpoint.accept()
                if self.channel:
                    self.connection = "STARTING_SHELL"
            if self.channel:
                self.channel.flush()
                for value in self.channel.receive():
                    self._event(value)
        except (ProbeError, OSError) as exc:
            self.last_error = str(exc) if isinstance(exc, ProbeError) else "ipc_failure"
            self._disconnect()
        self.command_risk_snapshot()

    def _event(self, value):
        kind = value.get("type")
        if kind == "ready":
            fields(value, {"type", "session"})
            require(self.session is None and self.connection == "STARTING_SHELL", "unexpected_ready")
            self.session = bounded_string(value["session"], 64)
            require(bool(re.fullmatch("[0-9a-f-]{36}", self.session)), "session_id")
            self.connection = "CONNECTED"
        elif kind == "output":
            fields(value, {"type", "start", "data"})
            require(self.connection == "CONNECTED", "unexpected_output")
            require(type(value["start"]) is int and value["start"] == self.output.end, "output_gap")
            try:
                raw = base64.b64decode(bounded_string(value["data"], FRAME_LIMIT), validate=True)
            except ValueError:
                raise ProbeError("invalid_output_encoding") from None
            require(len(raw) <= 65536, "output_chunk_limit")
            self.output.append(raw)
        elif kind == "result":
            fields(value, {"type", "id", "state", "offset"})
            # This is an acknowledgement of an already consumed local approval.
            # A bridge outage must not break the independent terminal IPC channel.
            require(self.review is not None and self.review.id == value["id"]
                    and self.review.session == self.session and self.connection == "CONNECTED", "stale_session")
            require(self.review_state == "CHECKING" and value["state"] in
                    {"OVERRIDE_REQUIRED", "TRANSFERRED", "TRANSFER_UNCERTAIN"}, "unexpected_result")
            require(type(value["offset"]) is int and value["offset"] >= 0, "invalid_offset")
            self.review_state = value["state"]
            self.offsets[value["id"]] = value["offset"]
            if value["state"] == "TRANSFERRED":
                if self.review.proposal_id and self.review.bridge_binding:
                    self.last_proposal_id = self.review.proposal_id
                    self.last_proposal_binding = self.review.bridge_binding[:4]
                    self.last_proposal_expiry = self.review.expires_at
                    self._record_transferred_submission(self.review)
                else:
                    self.last_proposal_id = None
                    self.last_proposal_binding = None
                    self.last_proposal_expiry = None
                first_transfer = self.last_transfer_offset is None
                self.last_transfer_offset = value["offset"]
                if first_transfer:
                    self.output_mode = "SINCE_LAST"
        elif kind == "ended":
            fields(value, {"type"})
            self._disconnect()
        else:
            raise ProbeError("unknown_event")

    def _disconnect(self):
        self._command_risk.invalidate()
        if self.channel:
            self.channel.close()
        self.channel = None
        self.connection = "DISCONNECTED"
        self.session = None
        if self.review_state == "CHECKING":
            self.review_state = "TRANSFER_UNCERTAIN"
        elif self.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}:
            self.review_state = "STALE"

    def close(self):
        if self.risk_setup:
            self.risk_setup.close()
        self.risk_requested = False
        self.risk_setup_pending = None
        self._command_risk.close()
        self.disconnect_bridge()
        self.pending_output = None
        if self.channel:
            try:
                self.channel.send({"type": "detach"})
                self.channel.flush()
            except (ProbeError, OSError):
                pass
        self._disconnect()
        if self.endpoint:
            self.endpoint.close()
            self.endpoint = None
        self.output = TranscriptRing()
        self.offsets.clear()
        self.last_transfer_offset = None
        self.output_mode = "LAST_20"
        self.inbox.clear()
        self.approved_outputs.clear()
        self.review = None
        self.review_state = "EMPTY"


class ScrollableText:
    """Native Tk Text: soft wrapping, vertical scrolling, named presentation tags."""
    def __init__(self, parent, *, readonly=False, height=8):
        import tkinter as tk
        from tkinter import ttk
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.text = tk.Text(self.frame, wrap="word", height=height, width=1,
                            font="TkFixedFont", undo=not readonly, padx=12, pady=12)
        vertical = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vertical.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        # Tags never change source bytes or introduce active markup.
        background = self.text.winfo_rgb(self.text.cget("background"))
        light = sum(background) / 3 > 32768
        self.text.tag_configure("control", foreground="#536579" if light else "#a8c2e0")
        self.text.tag_configure("unicode", underline=True)
        self.text.tag_configure("warning", foreground="#8b491f" if light else "#edbd8d")
        if readonly:
            self.text.configure(state="disabled")

    def show(self, fragments):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for text, tag in fragments:
            self.text.insert("end", text, (tag,))
        self.text.configure(state="disabled")

    def show_review(self, source, spans=()):
        """Project source characters to actual Tcl indices; never reconstruct payload."""
        if not spans:
            self.show(review_fragments(source))
            return
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        positions = []
        for char in source:
            positions.append(self.text.index("end-1c"))
            for display, tag in review_fragments(char):
                self.text.insert("end", display, (tag,))
        positions.append(self.text.index("end-1c"))
        light = sum(self.text.winfo_rgb(self.text.cget("background"))) > 3 * 32768
        colors = (("#e8eef4", "#263645"), ("#fff1cf", "#4a3c1d"),
                  ("#ffe1cf", "#4d2c1b"), ("#fbdadd", "#4b242c"))
        for index, (left, right, score) in enumerate(spans):
            tag = "risk-sub-" + str(index)
            self.text.tag_configure(tag, background=colors[sum(score > b for b in (20, 50, 80))][not light])
            self.text.tag_add(tag, positions[left], positions[right])
        self.text.tag_raise("sel")
        self.text.configure(state="disabled")


def connection_summary(bridge):
    labels = {"DISCONNECTED": "Nicht verbunden", "CONNECTING": "Verbindung wird aufgebaut",
              "PAIRING": "Kopplung läuft", "RETRYING": "Verbindung unterbrochen · erneuter Versuch",
              "BLOCKED": "Verbindung gesperrt"}
    if bridge.state != "CONNECTED":
        return labels.get(bridge.state, "Verbindung nicht verfügbar")
    if bridge.mode == "REMOTE":
        transport = "Remote-Bridge verbunden · authentifiziert\nTunnelbetrieb am Gateway"
    else:
        tunnel = {"STOPPED": "Tunnel gestoppt", "STARTING": "Tunnel startet",
                  "RUNNING_NOT_READY": "Client läuft · noch nicht bereit", "READY": "Tunnel-Client bereit"}
        transport = "Lokale Bridge verbunden\n" + tunnel.get(bridge.tunnel_state, "Tunnel nicht verfügbar")
    return transport + "\nChatGPT-Verbindung noch nicht verifiziert"


def chat_binding_presentation(bridge):
    """Return the display code, lifecycle status and whether deliberate copy is allowed."""
    binding = bridge.chat_binding
    if bridge.state != "CONNECTED":
        return "", "Verbindung getrennt · Chat-Code ausgeblendet", False
    if binding is None or bridge.chat_binding_received_at is None:
        error = " · Status nicht verfügbar" if bridge.chat_binding_error else ""
        return "", "Chat-Code wird geprüft" + error, False
    elapsed = max(0.0, time.monotonic() - bridge.chat_binding_received_at)
    if elapsed > 6:
        return "", "Chat-Code-Status wird erneut geprüft", False
    now = binding["server_time"] + elapsed
    if now >= binding["expires_at"] or binding["status"] == "EXPIRED":
        return "", "Abgelaufen · im Verbindungsdialog neuen Code erzeugen", False
    code = binding["code"]
    visible_code = code[:4] + "-" + code[4:] if type(code) is str else ""
    if binding["status"] == "LOCKED":
        seconds = max(1, int(binding["retry_at"] - now + .999))
        target = "Chatbindung" if binding["restriction_scope"] == "BINDING" else "AI-Zugang"
        return visible_code, "%s temporär gesperrt · noch %ds" % (target, seconds), False
    if binding["status"] == "BACKOFF":
        seconds = max(1, int(binding["retry_at"] - now + .999))
        return visible_code, "Wartezeit nach Fehlversuchen · noch %ds" % seconds, False
    expiry = datetime.fromtimestamp(binding["expires_at"], timezone.utc).astimezone().strftime("%d.%m. %H:%M")
    return visible_code, "Gültig bis %s · Code nur dem gewählten Chat geben" % expiry, True


def local_setup_summary(core):
    stages = {"PREPARING": "Client wird gesucht…", "INSTALLING": "Offizieller Client wird installiert…",
              "STARTING": "Lokale Verbindung wird vorbereitet…", "CONNECTING": "Lokale Verbindung wird aufgebaut…",
              "VALIDATING": "Client läuft · Runtime API key und Tunnel-Zugriff werden geprüft…",
              "FAILED": "Einrichtung fehlgeschlagen"}
    bridge = core.bridge_snapshot()
    if core.local_setup_state == "CONNECTED" and bridge.state == "CONNECTED" and bridge.tunnel_state == "READY":
        return "Verbindung bereit · Tunnel-Zugriff geprüft\nChatGPT-Round-Trip noch manuell prüfen"
    return stages.get(core.local_setup_state, connection_summary(bridge))


class ConnectionDialog:
    """One contextual setup window; all operations use existing local core actions."""
    def __init__(self, ui):
        self.ui, self.core = ui, ui.core
        self.tk, self.ttk = ui.tk, ui.ttk
        if self.core.connection_store is not None:
            # Also recover a protected claim saved while the settings window was closed.
            self.core.saved_connection = self.core.connection_store.load_connection()
        self.window = self.tk.Toplevel(ui.root)
        self.window.title("AI / MCP — Verbindung einrichten")
        self.window.geometry("%dx%d" % (min(860, ui.root.winfo_screenwidth() - 40),
                                       min(780, ui.root.winfo_screenheight() - 80)))
        self.window.minsize(640, 480)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(0, weight=1)
        self.busy = False
        self.last_state = None
        self.last_bridge_error = None
        self.last_tunnel_state = None
        self.last_tunnel_error = None
        self.revealed_binding = None
        self.fields, self.entries, self.buttons, self.labels = {}, {}, {}, []
        self.errors = {name: self.tk.StringVar(master=self.window) for name in ("bridge", "tunnel")}
        self.summary = self.tk.StringVar(master=self.window)
        self.binding = self.tk.StringVar(master=self.window)
        self.binding_status = self.tk.StringVar(master=self.window)
        self.failure_cause = self.tk.StringVar(master=self.window)
        self.technical_open = self.tk.BooleanVar(master=self.window, value=False)
        self.last_diagnostic = None
        saved = self.core.saved_connection or {"profile": None, "tunnel": None}
        profile = saved["profile"] or {}
        active = self.core.bridge_profile if self.core.bridge_state != "DISCONNECTED" else None
        self.location = self.tk.StringVar(master=self.window,
                                         value="LOCAL" if self.core.local_setup_state != "IDLE" else
                                         "REMOTE" if self.core.bridge_state == "PAIRING" else
                                         active.mode if active else "REMOTE" if profile else "LOCAL")
        self.compared = self.tk.BooleanVar(master=self.window, value=False)
        base = self.ttk.Style(self.window).lookup("TFrame", "background") or self.window.cget("background")
        self.canvas = self.tk.Canvas(self.window, highlightthickness=0, background=base)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = self.ttk.Scrollbar(self.window, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.content = self.ttk.Frame(self.canvas, padding=16)
        self.content.columnconfigure(0, weight=1)
        self.content_id = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.resize)
        self.window.bind("<MouseWheel>", self.wheel)
        self.window.bind("<Button-4>", self.wheel)
        self.window.bind("<Button-5>", self.wheel)
        self.window.bind("<FocusIn>", self.reveal_focus)

        self.intro = self.label(self.content, 0)
        choices = self.ttk.Frame(self.content)
        choices.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.location_buttons = []
        for value, text in (("LOCAL", "Lokal — dieser Rechner"), ("REMOTE", "Remote — anderer Rechner / Gateway")):
            button = self.ttk.Radiobutton(choices, text=text, variable=self.location, value=value)
            button.pack(anchor="w", pady=2)
            self.location_buttons.append(button)
        self.bridge_section = bridge_section = self.section(2, "1 · Bridge-Standort und Verbindung")
        self.local = self.ttk.Frame(bridge_section)
        self.remote = self.ttk.Frame(bridge_section)
        for panel in (self.local, self.remote):
            panel.grid(row=0, column=0, sticky="ew")
            panel.columnconfigure(1, weight=1)
        self.label(self.remote, 0, "Bridge und Tunnel laufen auf einem getrennten Gateway. Dort dieselbe Python-Datei "
                   "mit --bridge starten; benötigt wird cryptography. Ersetzen Sie die Beispielpfade und USER durch Ihre Remote-Werte.")
        self.entry(self.remote, 1, "url", "HTTPS-Bridge-Adresse", profile.get("url", "https://bridge.example:8443"))
        for row, key, title, value in (
                (2, "remote_python", "Python (Remote)", "python3"),
                (3, "remote_script", "Skriptpfad (Remote)", "/opt/aitl/admin_in_the_loop.py"),
                (4, "remote_state", "State-Verzeichnis (Remote)", "/home/USER/.local/state/aitl-bridge"),
                (5, "remote_bind", "Bind-IP (Remote)", "0.0.0.0")):
            self.entry(self.remote, row, key, title, value)
        self.label(self.remote, 6, "TLS-Name und Port folgen der HTTPS-Adresse. Controller → Gateway auf diesem Port "
                   "erlauben; kein eingehender Controller-Port. Der Start öffnet ein zweiminütiges Pairing-Fenster.")
        self.remote_command_box = ScrollableText(self.remote, readonly=True, height=4)
        self.remote_command_box.frame.grid(row=7, column=0, columnspan=2, sticky="ew", pady=6)
        self.button(self.remote, 8, "remote_copy", "Remote-Startbefehl kopieren", "bridge", self.copy_remote_command)
        self.label(self.remote, 9, "Die Gateway-Konsole zeigt Ports, vollständigen SPKI-Fingerprint und Kopplungscode. "
                   "Fingerprint unabhängig dort vergleichen, dann hier eingeben und bestätigen. Code: zwei Minuten, "
                   "einmalig, maximal fünf Fehlversuche. Bei Bedarf am Gateway neu mit --pair starten.")
        self.entry(self.remote, 10, "pin", "SPKI SHA-256 (vollständig)", profile.get("pin", ""))
        self.entry(self.remote, 11, "code", "Kopplungscode", secret=True)
        comparison = self.ttk.Checkbutton(self.remote, text="Vollständigen Fingerabdruck unabhängig am Gateway verglichen",
                                          variable=self.compared)
        comparison.grid(row=12, column=0, columnspan=2, sticky="ew", pady=6)
        self.button(self.remote, 13, "pair", "Koppeln und speichern", "bridge", self.pair)
        self.button(self.remote, 14, "saved_connect", "Gespeicherte Remote-Verbindung öffnen", "bridge", self.open_saved)
        self.label(bridge_section, 1, variable=self.errors["bridge"])

        self.tunnel_section = tunnel_section = self.section(3, "2 · OpenAI Secure MCP Tunnel")
        self.local_tunnel = self.ttk.Frame(tunnel_section)
        self.remote_tunnel = self.ttk.Frame(tunnel_section)
        for panel in (self.local_tunnel, self.remote_tunnel):
            panel.grid(row=0, column=0, sticky="ew")
            panel.columnconfigure(1, weight=1)
        self.local_inputs = self.ttk.Frame(self.local_tunnel)
        self.local_inputs.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.local_inputs.columnconfigure(1, weight=1)
        self.label(self.local_inputs, 0, "Zwei Werte aus der OpenAI Platform: Tunnel-ID unter Tunnels erstellen/ansehen, "
                   "separat einen Runtime API key unter Runtime API keys erstellen (Restricted, Tunnels Read + Use). "
                   "Kein App-Key oder Admin-Key. Auch dessen Benutzer benötigt Read + Use. Die App findet/installiert "
                   "den Client und richtet die lokale Verbindung automatisch ein. Den Tunnel der richtigen Platform-"
                   "Organisation und dem gewünschten ChatGPT-Workspace zuordnen.")
        config = saved["tunnel"] or {}
        self.entry(self.local_inputs, 1, "tunnel_id", "Tunnel-ID", config.get("tunnel_id", ""))
        self.entry(self.local_inputs, 2, "api_key", "Runtime API key", config.get("api_key", ""), secret=True)
        self.button(self.local_inputs, 3, "tunnel_help", "Tunnel-ID beschaffen · OpenAI Platform", "tunnel",
                    lambda: self.open_help(TUNNEL_SETTINGS_URL))
        self.button(self.local_inputs, 4, "key_help", "Runtime API key beschaffen · OpenAI Platform", "tunnel",
                    lambda: self.open_help(RUNTIME_KEYS_URL))
        self.button(self.local_tunnel, 1, "local_start", "Starten & testen", "tunnel", self.start_local)
        self.button(self.local_tunnel, 2, "local_stop", "Verbindung beenden", "tunnel", self.core.disconnect_bridge)
        self.local_notice = self.label(self.local_tunnel, 3, "Erst nach erfolgreicher Zugangsprüfung werden diese Werte geschützt gespeichert. "
                   "Lokale Zugangsdaten bleiben intern. Auf macOS installiert vorhandenes Homebrew den offiziellen "
                   "Client; ohne Homebrew wird der benötigte Installationsweg erklärt.")
        self.label(self.remote_tunnel, 0, "Den Tunnel betreibt der Gateway-Operator, nicht diese GUI. Dort: offizielles "
                   "Binary, Tunnel-ID (tunnel_ + 32 Hexzeichen), privater Runtime-Key und passende OpenAI-App-/Workspace-"
                   "Zuordnung. Runtime-Key-Datei benötigt Tunnels Read + Use. MCP_PORT ist der dynamisch ausgegebene "
                   "MCP-Port, nicht der HTTPS-Port; MCP bleibt auf Gateway-Loopback. ai-token liegt im Bridge-State-"
                   "Verzeichnis. Beispiele ersetzen; private Dateien 0600, Verzeichnisse 0700.")
        instructions = ScrollableText(self.remote_tunnel, readonly=True, height=9)
        instructions.frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=6)
        instructions.show((("/absoluter/gateway/pfad/tunnel-client-runtime run \\\n"
                            "  --control-plane.tunnel-id tunnel_IHRE_ID \\\n"
                            "  --control-plane.api-key file:/privater/pfad/runtime-key \\\n"
                            "  --mcp.server-url http://127.0.0.1:MCP_PORT/mcp \\\n"
                            "  --mcp.extra-headers 'X-AITL-AI-Key: file:/bridge-state/ai-token' \\\n"
                            "  --mcp.discovery-extra-headers 'X-AITL-AI-Key: file:/bridge-state/ai-token' \\\n"
                            "  --health.listen-addr 127.0.0.1:0 --log.level error --log.file /dev/null", "plain"),))
        self.label(tunnel_section, 1, variable=self.errors["tunnel"])
        self.cause_label = self.label(tunnel_section, 2, variable=self.failure_cause)
        self.technical_toggle = self.ttk.Checkbutton(tunnel_section, text="Technische Details anzeigen",
                                                    variable=self.technical_open, command=self.render)
        self.technical_toggle.grid(row=3, column=0, sticky="w", pady=5)
        self.technical_details = ScrollableText(tunnel_section, readonly=True, height=8)
        self.technical_details.frame.grid(row=4, column=0, sticky="ew", pady=6)

        self.chat_section = chat = self.section(4, "3 · Gewählten Chat bewusst binden")
        self.label(chat, 0, "Den 24 Stunden gültigen Chat-Code nur dem gewählten AI-Chat geben. Wer Code und "
                   "App-Zugriff besitzt, kann freigegebenen Output lesen. Dies ist keine vom Provider bestätigte Chat-ID.")
        self.button(chat, 1, "chat_show", "Chatbindung anzeigen", "bridge", self.show_binding)
        self.binding_entry = self.ttk.Entry(chat, textvariable=self.binding, state="readonly")
        self.binding_entry.grid(row=2, column=0, sticky="ew", pady=6)
        self.button(chat, 3, "chat_copy", "Chat-Code kopieren", "bridge", self.copy_binding)
        self.label(chat, 4, variable=self.binding_status)
        self.label(chat, 5, "Nach Ablauf oder Rotation den neuen Code erneut bewusst mitteilen und MCP-Event-Abos "
                   "im ausgewählten Chat erneuern. Rotation beendet auch den Legacy-Code.")
        self.button(chat, 6, "chat_rotate", "Neuen Chat-Code erzeugen", "bridge", self.core.rotate_chat_binding)
        self.label(chat, 7, "ChatGPT-Verbindung manuell prüfen: im gewählten Chat einen harmlosen Vorschlag für eine "
                   "Testshell anfordern, Command lokal prüfen/freigeben, Output separat wählen/prüfen/freigeben und "
                   "dessen Empfang im Chat bestätigen. Die App behauptet keine Provider-Verifikation.")
        self.details = self.tk.BooleanVar(master=self.window, value=False)
        self.details_button = self.ttk.Checkbutton(self.content, text="Weitere Optionen / lokaler Test",
                                                  variable=self.details, command=self.select_location)
        self.details_button.grid(row=5, column=0, sticky="w", pady=(0, 8))
        self.controls = controls = self.section(6, "Verbindung verwalten / lokaler Test")
        self.button(controls, 0, "reconnect", "Verbindung wiederherstellen", "bridge", self.core.reconnect_bridge)
        self.button(controls, 1, "disconnect", "Verbindung trennen", "bridge", self.core.disconnect_bridge)
        self.button(controls, 2, "offline", "Offline-Testvorschlag laden", "bridge",
                    lambda: self.core.add_proposal("printf 'Hello Admin in the Loop\\n'"))
        footer = self.ttk.Frame(self.window, padding=12)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.summary_label = self.ttk.Label(footer, textvariable=self.summary, justify="left", wraplength=540)
        self.summary_label.grid(row=0, column=0, sticky="ew")
        self.ttk.Button(footer, text="Schließen", command=self.window.destroy).grid(row=0, column=1, padx=(12, 0))
        self.label(footer, 1, "Schließen beendet nur dieses Fenster; eine laufende Kopplung/Verbindung bleibt bestehen.")
        self.location.trace_add("write", lambda *_: self.select_location())
        for key in ("url", "pin"):
            self.fields[key].trace_add("write", self.reset_comparison)
        for key in ("url", "remote_python", "remote_script", "remote_state", "remote_bind"):
            self.fields[key].trace_add("write", self.update_remote_command)
        for variable in (self.compared, self.fields["code"]):
            variable.trace_add("write", lambda *_: self.render())
        self.update_remote_command()
        self.select_location()
        self.window.bind("<Destroy>", self.destroyed, add="+")

    def destroyed(self, event):
        if event.widget is not self.window:
            return
        # Variable traces belong to this dialog, not to the lasting Tcl interpreter.
        for variable in (*self.fields.values(), self.location, self.compared, self.details):
            for mode, command in variable.trace_info():
                variable.trace_remove(mode, command)
        if self.ui.connection_dialog is self:
            self.ui.connection_dialog = None

    def section(self, row, title):
        frame = self.ttk.LabelFrame(self.content, text=title, padding=12)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        frame.columnconfigure(0, weight=1)
        return frame

    def label(self, panel, row, text="", variable=None):
        label = self.ttk.Label(panel, text=text, textvariable=variable, wraplength=560, justify="left")
        label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        self.labels.append(label)
        return label

    def entry(self, panel, row, key, title, value="", secret=False):
        self.ttk.Label(panel, text=title).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        self.fields[key] = self.tk.StringVar(master=self.window, value=value)
        self.entries[key] = self.ttk.Entry(panel, textvariable=self.fields[key], show="*" if secret else "")
        self.entries[key].grid(row=row, column=1, sticky="ew", pady=5)

    def button(self, panel, row, key, text, section, action):
        button = self.ttk.Button(panel, text=text, command=lambda: self.run(section, action))
        button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=5)
        self.buttons[key] = button

    def resize(self, event):
        self.canvas.itemconfigure(self.content_id, width=event.width)
        for label in self.labels:
            width = max(240, event.width - 64)
            if label.cget("wraplength") != width:
                label.configure(wraplength=width)
        self.summary_label.configure(wraplength=max(240, event.width - 140))

    def wheel(self, event):
        if isinstance(event.widget, (self.tk.Text, self.ttk.Combobox)):
            return
        step = -1 if event.num == 4 or getattr(event, "delta", 0) > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")
        return "break"

    def reveal_focus(self, event):
        widget = event.widget
        if not str(widget).startswith(str(self.content) + "."):
            return
        top = widget.winfo_rooty() - self.canvas.winfo_rooty()
        bottom = top + widget.winfo_height()
        height = max(1, self.content.winfo_height())
        offset = top if top < 0 else bottom - self.canvas.winfo_height() if bottom > self.canvas.winfo_height() else 0
        if offset:
            self.canvas.yview_moveto(self.canvas.yview()[0] + offset / height)

    @staticmethod
    def error_text(reason):
        messages = {
            "setup_busy": "Einrichtung läuft; bitte deren Ergebnis abwarten.",
            "invalid_bridge_url": "HTTPS-Adresse ohne Zugangsdaten, Pfad, Query oder Fragment eingeben.",
            "explicit_bridge_port_required": "Bridge-Adresse mit explizitem Port (1–65535) eingeben.",
            "verified_pin_required": "Vollständigen SPKI-Fingerprint mit 64 Hexzeichen eingeben.",
            "independent_fingerprint_comparison_required": "Fingerprint unabhängig am Gateway vergleichen und bestätigen.",
            "invalid_pair_code": "Den zwölfstelligen Gateway-Kopplungscode eingeben; Vierergruppen mit Bindestrichen sind erlaubt.",
            "network_http_403": "Kopplung nicht möglich. Code und Gateway-Fenster prüfen (zwei Minuten, maximal fünf Fehlversuche).",
            "network_http_401": "Bridge-Zugang nicht bestätigt oder widerrufen; gespeicherte Verbindung prüfen bzw. neu koppeln.",
            "network_unavailable": "Gateway/Bridge nicht erreichbar. Adresse, Port und Netzwerk prüfen. Falls bereits gespeichert: gespeicherte Verbindung öffnen.",
            "bridge_start_failed": "Lokale Bridge konnte nicht starten. Netzwerk, Laufzeit und verwendetes Python prüfen.",
            "cryptography_package_required": "cryptography wird im Controller-Python für Remote-TLS benötigt.",
            "invalid_tunnel_id": "Tunnel-ID im Format tunnel_ gefolgt von 32 kleinen Hexzeichen eingeben.",
            "invalid_tunnel_key": "Runtime-Key prüfen: 16–512 Zeichen aus Buchstaben, Ziffern, _ und -.",
            "explicit_tunnel_binary_required": "Absoluten Pfad zum separat installierten Tunnel-Client eingeben.",
            "unsafe_tunnel_binary": "Tunnel-Client muss eine ausführbare reguläre Datei sein, nicht gruppen-/weltbeschreibbar.",
            "tunnel_binary_missing": "Tunnel-Client unter dem angegebenen Pfad nicht gefunden.",
            "local_bridge_required": "Zuerst die lokale Bridge verbinden. Remote-Tunnel werden am Gateway betrieben.",
            "tunnel_config_required": "Zuerst gültige Tunnel-Einstellungen speichern.",
            "tunnel_process_exited": "Tunnel-Client wurde beendet. Client-Version/Laufzeit prüfen und erneut starten; keine neue erfolgreiche Einrichtung bestätigt.",
            "tunnel_health_unavailable": "Lokale Client-Bereitschaft noch nicht verfügbar; kein Provider-Verbindungsnachweis.",
            "no_saved_remote_connection": "Keine gespeicherte Remote-Verbindung vorhanden; Gateway-Pairing einrichten.",
            "saved_connection_mismatch": "Gespeicherte Verbindung passt nicht zur eingegebenen Adresse und zum Fingerprint.",
            "bridge_disconnected": "Zuerst eine authentifizierte Bridge-Verbindung herstellen.",
            "chat_binding_unavailable": "Chat-Code-Status wird geladen; Verbindung prüfen und erneut versuchen.",
            "chat_binding_generation_conflict": "Der Code wurde bereits geändert. Aktuellen Status abwarten; keine weitere Rotation gestartet.",
            "remote_command_not_ready": "HTTPS-Adresse mit Host/Port sowie gültige absolute Remote-Pfade und Bind-IP eingeben.",
            "homebrew_required": "Auf macOS unterstützt OpenAI Homebrew: Homebrew installieren (brew.sh), danach hier erneut starten. Alternativ den offiziellen Client mit brew install openai/tools/tunnel-client installieren; kein Gatekeeper-Bypass.",
            "client_download_failed": "Tunnel-Client konnte nicht heruntergeladen werden. Internetzugang zu GitHub prüfen und erneut starten.",
            "client_download_timeout": "Client-Download hat das Zeitlimit überschritten. Internetzugang zu GitHub prüfen und erneut starten.",
            "client_download_redirect": "Client-Download wurde auf ein nicht zugelassenes Ziel umgeleitet; Installation abgebrochen.",
            "client_archive_limit": "Client-Download/Archiv überschreitet das Größenlimit; Installation abgebrochen.",
            "unsafe_client_directory": "Client-Verzeichnis ist nicht privat oder gehört nicht dem Benutzer; Konfigurationsrechte prüfen.",
            "client_checksum_mismatch": "Client-Download stimmt nicht mit dem geprüften OpenAI-Release überein; nichts ausgeführt. Erneut versuchen.",
            "unsafe_client_archive": "Client-Archiv ungültig oder unsicher; Installation abgebrochen, nichts ausgeführt.",
            "client_install_failed": "Offizieller Tunnel-Client konnte nicht installiert/gestartet werden. Auf macOS Homebrew prüfen; danach erneut starten.",
            "client_version_unsupported": "Homebrew/Client-Prüfung: installierte Version ist zu alt für den benötigten Polling-Nachweis. Ursache und Version stehen unten; nicht mit lokalem READY fortfahren.",
            "client_probe_failed": "Installierter Client konnte nicht ausgeführt/geprüft werden. Exit-Code und Installer-/Client-Ausgabe stehen unten.",
            "client_install_timeout": "Client-Installation hat das Zeitlimit überschritten. Internet/Homebrew prüfen und erneut starten.",
            "client_platform_unsupported": "Für dieses Betriebssystem/diese Architektur ist keine automatische Client-Installation verfügbar.",
            "runtime_key_rejected": "OpenAI lehnt den Runtime API key ab (401). Einen gültigen Runtime API key aus der richtigen Organisation verwenden.",
            "runtime_permission_denied": "OpenAI verweigert Tunnel-Zugriff (403). Restricted-Key und Benutzer benötigen Tunnels Read + Use für diesen Tunnel.",
            "runtime_tunnel_not_found": "Tunnel nicht gefunden oder für diesen Key nicht sichtbar (404). Tunnel-ID und Platform-Organisation prüfen.",
            "runtime_rate_limited": "OpenAI begrenzt die Anfragen (429). Später erneut versuchen; keine neuen Werte gespeichert.",
            "runtime_network_unavailable": "Client erreicht die OpenAI-Control-Plane nicht erfolgreich. Internetzugang zu api.openai.com:443 prüfen.",
            "client_health_unsupported": "Client liefert keinen unterstützten Nachweis erfolgreichen Pollings. Offiziellen Tunnel-Client ab v0.0.15 verwenden; keine Credentials gespeichert.",
            "client_health_unavailable": "Lokaler Client liefert keinen erfolgreichen Statusnachweis. Laufzeit prüfen und erneut testen; noch keine dauerhafte Speicherung.",
            "local_setup_timeout": "Verbindungstest nicht rechtzeitig erfolgreich. Netzwerk, Tunnel-ID und Tunnels Read + Use prüfen; keine neuen Werte gespeichert.",
            "local_preparation_failed": "Lokale Vorbereitung fehlgeschlagen. Client-Installation, private Konfigurationsrechte und Python-Laufzeit prüfen.",
            "browser_unavailable": "Systembrowser konnte nicht geöffnet werden. Offizielle Links: " + TUNNEL_SETTINGS_URL + " und " + RUNTIME_KEYS_URL,
        }
        if reason.startswith("tls_"):
            return "TLS-Prüfung fehlgeschlagen. Vollständigen Pin, Zertifikatsnamen und Gültigkeit am Gateway prüfen."
        return messages.get(reason, "Einrichtung fehlgeschlagen. Eingaben, Berechtigungen und Laufzeit prüfen; keine Inhaltslogs.")

    def run(self, section, action):
        try:
            require(not self.busy and not self.pairing(), "setup_busy")
            self.busy = True
            self.render()
            self.window.update_idletasks()
            self.errors[section].set("")
            action()
        except FileNotFoundError:
            self.errors[section].set(self.error_text("tunnel_binary_missing" if section == "tunnel" else "bridge_start_failed"))
        except ProbeError as exc:
            self.errors[section].set(self.error_text(str(exc)))
        except Exception:
            self.errors[section].set(self.error_text("setup_failed"))
        finally:
            self.busy = False
            self.render()
            self.ui.render()

    def pairing(self):
        return self.core.pair_thread is not None and self.core.pair_thread.is_alive()

    def select_location(self):
        local = self.location.get() == "LOCAL"
        if not local:
            self.errors["tunnel"].set("")  # Local test errors do not describe a gateway-managed tunnel.
        self.intro.configure(text="OpenAI-Anbindung auf diesem Rechner: zwei Platform-Werte und Starten & testen. "
                             "Terminal und Freigaben bleiben unabhängig." if local else
                             "Zuerst den Standort der Bridge wählen, dann die OpenAI-Anbindung einrichten. "
                             "Alle Schritte bleiben in diesem Fenster; Terminal und Freigaben bleiben unabhängig.")
        self.bridge_section.grid_remove() if local else self.bridge_section.grid()
        self.tunnel_section.configure(text="OpenAI Secure MCP Tunnel" if local else "2 · OpenAI Secure MCP Tunnel")
        self.details_button.grid() if local else self.details_button.grid_remove()
        self.controls.grid() if not local or self.details.get() else self.controls.grid_remove()
        for panel, visible in ((self.local, local), (self.remote, not local),
                               (self.local_tunnel, local), (self.remote_tunnel, not local)):
            panel.grid() if visible else panel.grid_remove()
        self.canvas.yview_moveto(0)
        self.render()

    def reset_comparison(self, *_):
        self.compared.set(False)
        self.render()

    def update_remote_command(self, *_):
        self.remote_command = ""
        try:
            parsed = urlsplit(self.fields["url"].get())
            require(parsed.scheme == "https" and parsed.hostname and parsed.port
                    and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                    and not parsed.query and not parsed.fragment, "invalid_bridge_url")
            self.remote_command = remote_bridge_command(
                self.fields["remote_python"].get(), self.fields["remote_script"].get(),
                self.fields["remote_state"].get(), self.fields["remote_bind"].get(), parsed.port, parsed.hostname)
        except (ProbeError, ValueError):
            pass
        self.remote_command_box.show(((self.remote_command or self.error_text("remote_command_not_ready"), "plain"),))
        self.render()

    def copy_remote_command(self):
        require(bool(self.remote_command), "remote_command_not_ready")
        self.window.clipboard_clear()
        self.window.clipboard_append(self.remote_command)

    def pair(self):
        require(self.compared.get(), "independent_fingerprint_comparison_required")
        pin = re.sub(r"[\s:-]", "", bounded_string(self.fields["pin"].get(), 128)).lower()
        check_profile(ConnectionProfile(self.fields["url"].get().rstrip("/"), pin, "0" * 32, "0" * 32, "0" * 64))
        code = bounded_string(self.fields["code"].get(), 16)
        require(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{12}", code.replace("-", "").upper()), "invalid_pair_code")
        self.core.begin_pair_remote(self.fields["url"].get(), pin, code)
        self.fields["code"].set("")

    def refresh_saved(self):
        if self.core.connection_store is not None:
            self.core.saved_connection = self.core.connection_store.load_connection()

    def saved_matches(self):
        saved = self.core.saved_connection
        profile = saved["profile"] if saved else None
        return bool(profile and profile["url"].rstrip("/") == self.fields["url"].get().rstrip("/")
                    and profile["pin"] == re.sub(r"[\s:-]", "", self.fields["pin"].get()).lower())

    def open_saved(self):
        self.refresh_saved()
        require(self.core.saved_connection and self.core.saved_connection["profile"], "no_saved_remote_connection")
        require(self.saved_matches(), "saved_connection_mismatch")
        self.core.connect_saved_bridge()

    def start_local(self):
        self.core.begin_local_setup(self.fields["tunnel_id"].get(), self.fields["api_key"].get())

    def open_help(self, url):
        import webbrowser
        require(webbrowser.open(url), "browser_unavailable")

    def show_binding(self):
        bridge = self.core.bridge_snapshot()
        code, _, _ = chat_binding_presentation(bridge)
        require(bridge.state == "CONNECTED" and code, "chat_binding_unavailable")
        self.revealed_binding = (bridge.instance, bridge.generation, bridge.conversation,
                                 bridge.chat_binding["generation"])
        self.binding.set(code)

    def copy_binding(self):
        bridge = self.core.bridge_snapshot()
        code, _, can_copy = chat_binding_presentation(bridge)
        require(can_copy and self.binding.get() == code, "chat_binding_unavailable")
        self.window.clipboard_clear()
        self.window.clipboard_append(code)

    def render(self):
        bridge = self.core.bridge_snapshot()
        if bridge.state == "BLOCKED" and self.last_state == "PAIRING":
            try:
                self.refresh_saved()
            except ProbeError as exc:
                self.errors["bridge"].set(self.error_text(str(exc)))
        previous_state = self.last_state
        self.last_state = bridge.state
        local = self.location.get() == "LOCAL"
        ready = local and self.core.local_setup_state == "CONNECTED" and bridge.state == "CONNECTED" and bridge.tunnel_state == "READY"
        self.summary.set(local_setup_summary(self.core) if local else connection_summary(bridge))
        self.local_inputs.grid_remove() if ready else self.local_inputs.grid()
        self.local_notice.grid_remove() if ready else self.local_notice.grid()
        self.buttons["local_start"].grid_remove() if ready else self.buttons["local_start"].grid()
        self.buttons["local_stop"].grid() if local and self.core.local_setup_state == "CONNECTED" and self.core.tunnel is not None and bridge.tunnel_state != "STOPPED" else self.buttons["local_stop"].grid_remove()
        self.chat_section.grid() if not local or ready or self.details.get() else self.chat_section.grid_remove()
        for name in ("reconnect", "disconnect"):
            self.buttons[name].grid_remove() if local else self.buttons[name].grid()
        code, binding_status, _ = chat_binding_presentation(bridge)
        self.binding_status.set(binding_status)
        active_binding = (bridge.instance, bridge.generation, bridge.conversation,
                          bridge.chat_binding["generation"] if bridge.chat_binding else None)
        if bridge.state != "CONNECTED" or not code or active_binding != self.revealed_binding:
            self.binding.set("")
            self.revealed_binding = None
        visible = bool(self.binding.get())
        self.binding_entry.grid() if visible else self.binding_entry.grid_remove()
        self.buttons["chat_copy"].grid() if visible else self.buttons["chat_copy"].grid_remove()
        busy = self.busy or self.pairing() or bridge.state in {"PAIRING", "CONNECTING"} or self.core.local_setup is not None or self.core.local_pending is not None
        for key in ("tunnel_id", "api_key"):
            self.entries[key].configure(state="disabled" if busy and local else "normal")
        available = {
            "local_start": local, "local_stop": local and self.core.tunnel is not None,
            "tunnel_help": True, "key_help": True,
            "pair": not local and self.compared.get() and bool(self.fields["code"].get()),
            "saved_connect": not local and self.saved_matches(), "remote_copy": bool(self.remote_command),
            "chat_show": bridge.state == "CONNECTED" and bool(code), "chat_copy": visible
                and chat_binding_presentation(bridge)[2],
            "chat_rotate": bridge.state == "CONNECTED" and bridge.chat_binding is not None
                and not bridge.chat_binding_busy,
            "reconnect": self.core.bridge_profile is not None and
                (self.core.bridge_profile.mode == "REMOTE" or self.core.bridge_process is not None
                 and self.core.bridge_process.process.poll() is None),
            "disconnect": bridge.state != "DISCONNECTED" or self.core.bridge_process is not None,
            "offline": bridge.state == "DISCONNECTED" and not self.core.inbox,
        }
        for name, button in self.buttons.items():
            button.configure(state="normal" if available[name] and not busy else "disabled")
        for button in self.location_buttons:
            button.configure(state="disabled" if busy else "normal")
        if bridge.last_error and bridge.last_error != self.last_bridge_error:
            self.errors["bridge"].set(self.error_text(bridge.last_error))
        elif bridge.state == "CONNECTED" and previous_state != "CONNECTED":
            self.errors["bridge"].set("Bridge-Zertifikat läuft in weniger als 30 Tagen ab." if bridge.certificate_warning else "")
        self.last_bridge_error = bridge.last_error
        tunnel_error = self.core.tunnel.error if self.core.tunnel else None
        if bridge.tunnel_state == "READY" and self.last_tunnel_state != "READY":
            self.errors["tunnel"].set("")
        elif tunnel_error and bridge.tunnel_state != "READY" and tunnel_error != self.last_tunnel_error:
            self.errors["tunnel"].set(self.error_text(tunnel_error))
        self.last_tunnel_state, self.last_tunnel_error = bridge.tunnel_state, tunnel_error
        if local and self.core.local_setup_error and self.core.local_setup_state == "FAILED":
            self.errors["tunnel"].set(self.error_text(self.core.local_setup_error))
        diagnostic = self.core.local_setup_diagnostic if local and self.core.local_setup_state == "FAILED" else None
        if diagnostic is not None:
            self.failure_cause.set(diagnostic.cause())
            self.cause_label.grid()
            self.technical_toggle.grid()
            if diagnostic != self.last_diagnostic:
                self.technical_details.show(((diagnostic.text(), "plain"),))
                self.last_diagnostic = diagnostic
            self.technical_details.frame.grid() if self.technical_open.get() else self.technical_details.frame.grid_remove()
        else:
            self.cause_label.grid_remove()
            self.technical_toggle.grid_remove()
            self.technical_details.frame.grid_remove()
            self.technical_open.set(False)


# Cropped original prompt/circuit artwork, embedded for single-file downloads.
# 80x64 PNGs; light has dark backing, dark has transparent outer padding.
_SECTION_LOGO_PNG = {
    "terminal": {
        "light": (
            "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"
            "AsHo8fZoyYmCAAAAAElFTkSuQmCC"
        ),
        "dark": (
            "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"
        ),
    },
    "ai": {
        "light": (
            "iVBORw0KGgoAAAANSUhEUgAAAFAAAABACAIAAAACpWdhAAANg0lEQVRo3u1b7ZOV5Xn/Xdf9nD1n2VeWJYuCoIsCshBYETTRmTRt"
            "Z9rGtBpEMSHJpJN+yEunnWam7Zd+iOOHzuSb03+gH9qmmaZGk6AgUMCohGpURERNDERHRWBh2V12zznPc1+/frif5znP2V2GnnWB"
            "afWeYTjnec7z8ruvt9/1stKxcAAfp6XX+gU+AfwJ4E8Af6QVtXoBzaYdAADIjK8EIBAWfirZ6cs/hQQJQEREJLs1AQEBYf5E0dZk"
            "1jLgGUsynNIMSopHGy9Y/DgbUACV9vZF/Yvb29tr1erIyMjk5AQJkeIDL32HKw+4CPvyu5IjA1PxgWQK1kSwdt3Gu3/vD5cuu6FS"
            "qZhPzpz+8L8PPffC4eer1WoQ9YxbtbZcW3tna1fwss+SaZ847VoRQILIRASioqLqVN1dn/v9L35p+8B117uopCKlUtTb17dm7VBn"
            "V/eJ377tvQ+Ysxume3bNAadQJfs5SZrRPGmkBYkKBCDNSAu/Xffpjffd/2ClUiGpKgQImPcArl+6rDo1efLEieabC0CRK27DRbck"
            "zcczRwIAMFr7go6bBm9euuwG59S8NyMAVY1KkaoD6BPvzQA4F61dt35Bx4Ik8SJCEhAzA0EYIJ++bfOvXjg8dmE0EzILu3plAcuM"
            "Dw3MIulLkLbk+qVfuHfbLatvrVQqKjCj90bAqTqXWq83SzyDSEGapxaUlhAIjQKgb1H/wMCSC6PnMx2eo9+aL6eFDCdEQGBgyfXb"
            "d3xj2Y2DZkySxKnQGEB4780yfSdJEPDeVERUQIS9yLyxKOjNnIsq7e2ggXN30XMDfMm4ErScZosHljzw5a8O3nxL4r2qikBVAscx"
            "S0EGXVANRm6kADAzF/RZ1GjB/s1oZD2uj4+Pk+TlXmPeAV/yMQRoflH/p7Y++JXBW1YniQ8KnsTxyNnTPolJeu8T780bRJxq2I6w"
            "CwsXLe7s6rYg99Q6KBBVOnFnTp8/NzLiolJQjGuo0o1nk7ZwUf+Xtu9YuerWJPEACKnXpvY89bMXf/l8iEhm3nwSPLOoqigkWK/f"
            "uPmOP7t/eykqQSAQksFaVRTAqy+9ODExrqo0g/wvY8Usaw5hyZrJTuquadbb17ftoa+tWbeeZoES1uu1vbt3PrNvd61W9Uni4zhJ"
            "Ym/ejObNzHuf+CT2PvE+OX3qg/ZK+/IVNzoXZaELqmq0Fw8f2r9ndxzXc5g5b2s1LLUO2Io0L306zXoWLtz20NfWrt8Q3lJE4npt"
            "39NPHtz3NL2JaFBRQALnSP/LbyFqZife/vXY6GhnV1e5XAGkXqu//967+/fsOrBnd21qCmDmzBoRsFXiIa1WPGi+iUsJaOzq6dm6"
            "fcfQhmEQKiIiSRLv3bVz7+4nzSeS04SibLL7pZuAQGlIoqOza/HAkkqlfWpq8uyHpyYmxrL9CswMWUQgWk8eWgfsfWq1aShFd0/v"
            "/Q9+ZWjDsKX5jZqPD+59+umnfh7H9UwCM5QxB9yUX6Q/C/45bKgUrQcZn2ZGgK54tpQ9TEgjO7t67tu2fePtm733AqEhTuKDe3fv"
            "2bXTJ3GuxtNxpqS6GTjy2MsGztlyhaDXBdQtrDkUAAIVovdJuVy+596t64c3mZmIRKoqfO7gvr27diZxPQM2HVZ6j5STpcEbItNl"
            "H1KMHLZk9Du977Q8/AoCTg2pfUHHPfc9MLz5zpQzEmb+2YP/tWfXk4lPUk2exaOw+QMLxwTTA45Mk6AU8u25BaaWVVoAqHYu6Pri"
            "vVtvv/OzgAbmVI/j5w7u273zp/V6fUaYLBowC+ld0eWy8ACkyGU6rZphCS2vOdW0yOXLV2y6fUtbqaQqqkrw0C8OPPXTn1QnJ9Gg"
            "f0WcmS0yhzw9bZ72iMbJBuqiJswpV5pLHAYBOX/+/MXJiRU3rSxXKk71xcOHfv7EY7VqVVOfySaQxdApuSuSLMhkMr0UXyRphCdI"
            "EYWG6zLIV7oAYDSA3ifvnjx54cLo4ODNR4+8/PhjP65OXUxTgVQsOT+RRqFjFkfTsMmGRy9ABSGLyljTI6t60NumMaVGiOSmf5WI"
            "R+BKIu6G5StGzp6ZGB8vFKdmEdGMxzb+MQ8wbApCNENHyR5YZQ8OYWUvKqo1ut+M4V+P4/E3OBUH4qVRa26odcA0KShSKMEUqmtN"
            "UssvQsOqpSDQcEPOchnJjij+h8/bjjVsowjFUVSdRnZB+Ojz0aO/RGIqqlGppfdvWaWlmQSLqqg2EexZVEwaPssTICKFCFShQhE4"
            "gQpEoYKQOQttx5B9704pUWAAQQPNvDfnuX6RvHVW3zwPVVXX0vvPQ8WDbE4nWFTMxkEAKEd293XJXcsw0AUVaIANiEIID3hLOUWb"
            "cvMAy6ZGQAHSGIALjd1tfGgtDryv9ZaD8byVeArWWPieb4lROtr8326xbw7bAoX3MA8gE6kAgAlIIUSEzpEJzKd8miKqIbkQE4ja"
            "rX2yuF3fq7XKLucNMAv2SRTlKwKgsxT/zebkL4alTDCWAI4UD3gyDzMpm3ZqxlCpFYoxDT8KgTh1QsfyAra5vMR7VQEXZMksICFP"
            "qABBVyX5+zuSrw+RnlUgEpYq4pxQjEYjSJgFkwYEBlFCDQhFMAtqAoFoBIlKUvaTo6j5ObztfNhwijmDWkxuDdJR9n93p31zGC6W"
            "GPSGOt0z7+h7U+JcKIPBJzQTp2nKRBOFfXaZrekXS0gABkCgMHhFopRXTrmRemoLVxlwjjnwgdxjmTe0qf/LjclXV5M1mEpbSRJ1"
            "//5y6ZFnOR6LaqH/wNT/k7SERnxhtT16D/tKcB70oJBCD4rU3x8p//PLqNocmPEcqOX0leV4Uiy3CIFy5P/qjvhbm0USxKH6DPfD"
            "4+V/PCSjNQhBCq2RBQMCChDqG3JyVMZjDC9ldzmkLKELhd+edw/v1/0nEWy8xZpWy8RjFvEGC0xbgRmDqpT8X2+pfec2qkni4UkT"
            "92+vlH5wWMcTaHP2PkMxg25TwS3LdPuQbey3zpKMVfWFU/LDYzhyhmSo2au7wkzrkoCzVxeotFf89+5Ivr3Jl2ILAoxV/uVo6ZH9"
            "MlYXp9IctxuYm9vnBOBNIuXCMhY4THodTeBBJUKfClB31YlHJhASBqO0lfjdYfv2ZmvzIFSgdPqfx6IfHMKEMXJNnfLGrs3IKYJd"
            "qMIoI1WcBSBB48Plc6sAzEtYEonUq5kBKvbnQ/atDdJWh0RO25AIfvRa6eFf4FyNmpUsZhT2JHfybD6Vo9f8VFYFmFMhYF7CEvkn"
            "K23b6oQJysoNS9EGqSWMQET649ejh5/BuSqF03OKIrwcwKyDIMVadFNb9hpRSw4udH+8yjsz84xNKEZhFfKTI9H3D2I0pmKml8rS"
            "ZEE2wpKVB2ZUsvK9bYI5F52eH6Yl3sQnDh4wlpRQUNyPXo++f8BGqhKpQDCj0Jhm/KFkmRpxoabX3MtpoupzEe38AQbgaz7xACKW"
            "VJxzsejjR6NHnuP5OiIFQFJygGiy3kyfM6Gn9VpKsebDGf2dvDx+TQDLSx/KP70gThA5RoJ3L0ZPvClnq3DaGNKZ5mjyAmZziymf"
            "DDFYOrXS1Fv4iEM88xGHQ9MazKbJgstVDWlsk72iINLwJWuNNwZHaBDt7Ozs6u4WyPj42MTFi1ncY+NWGXC5BnFYsgZPqFCHigAM"
            "Jg2JFTtMWRcxbRWlIywAzZvv7u27+3N/MLR+Q09vr1MdGzt/9MiRZw8euDB6LnVvH23NA5duhEdSshcqNPuafW5DsyVvBUpGS3sX"
            "Ltr25a9v+cxdPT095XK5rdTW2dVz400rFw9cd+I3b01NTjQx5/CIFptp8wEYMPNm3sxSyalKKFBlU5NhHqtRhM/6uiTJtOtYKpf/"
            "dOsDGzbdHpInI80s8d6I/sUDnd1db71xPBsLkNyxX4U5rVkEvGJw5Zo168J+axhkACzFmaqxZKJESJTSBYAqos51dXVtvG2TFP1z"
            "MG9ShBtv23T0lZeOHX01bbmw2JS6uoAB3LB88PN/dI/TRq2dzIWZtUUyqTCf5cnOhn6NhqqPsRGfARWBilONyu0rbrzp2NEjTd2p"
            "1tc8hSURhZmnsSFRUQmdl9RNE+Zp+WSw5FUsMU8zC/aoaZE/a8Vkym9EpVIJbYCsso3Ww/A8AY4ip+q8T+tPQXXzqaQgMSOhqehT"
            "zxXAGT2bg3W2jXlpwEjzdmFszLxX57K28VWa05plnTl9+sgrv0qSxHsLA4gahpJENB9GBM3IdPRKXRQFsmTeJ4k3s3K5fPOqNZVK"
            "hQFM1icPbqpeq7578kTuz+fcNJ0PLi04/tqRN44dJdjcX5rJ/BrKqOog2awtDWAUle7fvmPTls+EycR8plJFXOSOv/bqO797x5Xa"
            "QM5h0iFf8xCWGGQX6qlM65XhRGqvqY/KzpIEjWbeYJbHKfN26oMPPjUw0N+/OIwPI7RyRE68/esnHvuP0dFzmD7XcRXmtGbH3OQ5"
            "025b/vcKks1oIM8TmrvhmTCnJifePP76xPh4uVIRkVqteurU+4eePfjUzx4/c/pDQRiLy7Y5EJcr3S6dHW429p2iUYEojaTlbPCy"
            "LyfZbCqBzq7u3t4+AKOj5y6Oj2E6o8rHU6/8nNb/9fWx+7ulTwD/f18fO8D/A2Mkid6jYiMEAAAAAElFTkSuQmCC"
        ),
        "dark": (
            "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"
            "8SuP/wFHteI2ROwuywAAAABJRU5ErkJggg=="
        ),
    },
}


# Twemoji v14.0.2 graphics by Twitter and contributors, CC BY 4.0, provided AS IS.
# https://creativecommons.org/licenses/by/4.0/ (graphics, not the Python code).
# Sources: https://github.com/twitter/twemoji/tree/v14.0.2/assets/72x72
# 1f44c.png (OK hand), 270b.png (raised hand); resized from 72x72 to 24x24.
_BUTTON_HAND_PNG = {
    "ok": (
        "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAMAAADXqc3KAAAB7FBMVEUAAAD/317/3F3FAATsi0HulUTukUPvmUbrhj/+2Vz91lv+"
        "21z+21z/3F3/3F3vlkXHAAf2uFD+21z/3F3/3F3vlkXtjkL/3V3+21zvmEX4wlT+2lz/213/3F3ulETypUruk0T+2lz/3F3+21z/"
        "3F32tU/vmEX3ulH+2lz/213vmUbwnEfulUT91Fr/3V3+3F3/3F3/3F3/3F3/3F3+2Vzvl0X1sk7+21z/3F3/3F3/3F3/3F3/213/"
        "317zrU3ukkP/3l3/213/3F3/3F3/3F3/3F3/3F3+21z/4F7yqEv2t1D/3F3/3F3/3F3/3F3+21z/3F3/3F3/3F3/3F3/3F3/3F3/"
        "213/3F3/3F3/3F3/3F3+21z/3F3/3F3/3F3/3F3/3F3/3F3+21z/3F3/3F3/3F3/3F3/3F3/21z/3F3/3F3/3F3/3F3/3F3/3F3/"
        "3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/"
        "3F3/3F3/3F3/3F3/3F3/3F3/3V3xokn+21zukUP/4F7wnkftkUP/4V7vmUbukkP+3F3vl0X7zljsjEH+2lzzp0vxoEj/3l31s0/8"
        "0ln+2Fv/211XKJguAAAAjXRSTlMAAAAAAAAAAAACDsORAgEBB6r9QQMEZPu/V/39WAQBCtv+/dUJB3r8/G8CHO3+/uUXCl26eIv8"
        "/Icae9/9tj/x/uYRVPj++/nW3P4q7/r1+6DE+cBPEpfdCDSwiSgGvtwZBf4paNOdEJfqDz07vUpDuPx3HlDu2ivPilm3TTPlGN72"
        "G9uSHX9bv+3mwyzPNLBNAAABsUlEQVQoz2WR51vUQBDGjxE7uwoGC7qWgFioInhiRTFRwAM9lCZ3ChakCQdYsKFSLPDiiqIRFAv/"
        "KLOJ6HOPky/Zefc38+5MKPQ31qasW480ElKGkmLDio3p0xnYRBZRkpCZuvn9lgy99T8kc+W2D1kz27GDVBIid67atfvjHltn59A/"
        "hCRJtTc199O+2f04wEigSElCSTq4Oi//c0GhLirmNst5SYdKDpeuKftyZCbsHSUVCGyw/NhxnNAnT53+Olehz5z1EcklK8/BcZ3z"
        "+kLVfDUbqwwQQTUXEQFqvTrn2/eFH2F9yReYuIwo6q9cbUBdoffzV4VubGJb0qJm2GghKegawp73GzrSSpbxFIvj+g1qsxQr7Roa"
        "7s1AuHX7TgcJIobvsuIsdnaRCFnUDXSaNxl7PQDX7TV2Fd3j1n1mDNyln9MJDBhXggZrHf6NKRmjITj83R80BF+LAw8eknHxiAEX"
        "w8EzFD3mg/vkqfWs3geejwQT4Zv9iDrAi5cm76JkeeyCXo0iEbHhJGzbxhhZ8s9wBY3H/SIc0QnJOwhWa9ahXr95C/vd5PAU3/cL"
        "LQGT13ftJtFu8wAAAABJRU5ErkJggg=="
    ),
    "stop": (
        "iVBORw0KGgoAAAANSUhEUgAAABgAAAAYCAMAAADXqc3KAAAB4FBMVEUAAAD2tk///9X////jYzP/3F3/8mT/4F7vlkXzqUv+21z/"
        "3l32tk/wm0bwnUf////7zFf+21z+2Fv/3F34u1L/+Gb91lv/5mDxoEj+21z91Vr+21z/3F36xlXxoEj/41/4vlL4v1P+2Fv3u1L/"
        "3F3zq0zwnUf/41/4v1P/317/3F3/3F3+2lz/5WDwnUf/41/1sU7/317/3F3/3F3/3170r03/317/3F3/3F3/3l3/3F3/3F3/3V3+"
        "21z+2lz+2lz/3F3/3F3/21z+3F3+4F7/3F3/3F3/3F3/3F33vFL//5r+21z/3F3xokn2tlD/3F3/3F3/3F3/3F3+21z/3F3/3F3/"
        "3F3/3F3/213/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3/3F3+21z/3F3+21z+21z/3F3/"
        "3F3/3V35wlT/4F7801n/4V/+21z/3F380ln4v1P/41/1sk71sU74wFP/5F/4vlL4vVL80Vn0rk30r03/4l/6yVb/4V791Vr7zVj+"
        "2Vz+3V3/21z/3l3+3F3+2lz/3177y1f2tlDzqEv91lvxn0jzqUv80Fn+3l3/213/3Fzxokn1r033vVLxoUnypUr5xFT+11uosEWY"
        "AAAAcHRSTlMAAAAAAAAAAAAAAAABAgEBkLoBAQEBTndn7xFDRwdk/vP9554EBKv4/vlWBXNfrfz++1VP+PX8VwP8PQ38/FYDlqf8"
        "/Fp093/8WwPmpv7Pvc8CO/36xkL+/JkR5/1Rk/vUHFRD9oQ31vNrDG7K998kK401cgAAAadJREFUKM9t0vtbDFEYB/AhryKlHBRd"
        "KJNL61IrNdSWoqxurDBoRRfKfdp6v7NdpVo02ybbzS3+VedMa57H8zQ/vp/zne85Z0bTNG3fjqzs/TtzrNy0XQcECc17Dh7afTgv"
        "fzhy5GhBYboHcklRRvHIseMlo6UnGAWkewFRtuekfep0dMweP8PlHgjynT133r4QlVBRyX4PdLrIVZfsMQUT1f9BDVdNbAt+rt4e"
        "yl2YnJTlLghDV6dxYdyemn4/NfNBgc8gIQRtwezM3Px87GP0sgK64q9NJeo+fcaC5SAe4Pq9DVeBRtkkAU3xRYcZnLi2dD2zGS24"
        "sQUcVFPwYizw5WZrG9AuW9xEEHDAcV7u+Np5i/l2LYVUuV8B2EqurK7F1xm4Qzr9S2zwN+v7j7l17rrL9ygk3yThPkwgEfv5K8Ky"
        "6wEeyjOSpoXoUTdM5qXN35EENhDGYwnq2nXqwRO2/mwmLXbk/KmPVEATIertRsBKzqotm0H0kZH6UAb1D3C4Kwk2w2E8I0MIT54P"
        "OuaC3CiGXqg5uUBKXr56/Qbm2553pKd+oL+kn5PYyJwoiAAAAABJRU5ErkJggg=="
    ),
}


def _logo_variant(root, background):
    rgb = tuple(value // 256 for value in root.winfo_rgb(background))
    return "light" if sum(rgb) > 384 else "dark"


class ReviewUI:
    """Three columns; immutable tagged review. Context editing creates a new revision."""
    def __init__(self, root, core):
        import tkinter as tk
        from tkinter import ttk, messagebox, font
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root, self.core = root, core
        self.last_snapshot = None
        self.last_binding_rows = None
        self.last_binding_review = None
        self.binding_touched = False
        self.launcher = None
        self.connection_dialog = None
        self.risk_dialog = None
        self.risk_details_box = None
        self.risk_render_key = None
        self.risk_confirm_pending = False
        self.status_labels = []
        root.title("Admin in the Loop · v" + BRIDGE_SERVER_INFO["version"])
        width = min(1360, root.winfo_screenwidth())
        height = min(850, max(600, root.winfo_screenheight() - 80))
        root.geometry("%dx%d+0+0" % (width, height))
        root.minsize(960, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.bind("<F11>", lambda _: root.attributes("-fullscreen", not root.attributes("-fullscreen")))
        root.bind("<Escape>", lambda _: root.attributes("-fullscreen", False))
        style = ttk.Style(root)
        base = style.lookup("TFrame", "background") or root.cget("background")
        self.logo_variant = _logo_variant(root, base)
        self.section_images = {}
        self.section_logo_labels = {}
        self.header_bar = ttk.Frame(root)
        self.header_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(8, 4))
        self.header_bar.columnconfigure(1, weight=1)
        self.logo_label = ttk.Label(self.header_bar, text="Admin in the Loop",
                                    font=("TkDefaultFont", 13))
        self.logo_label.grid(row=0, column=0, sticky="w")
        ttk.Frame(self.header_bar).grid(row=0, column=1, sticky="nsew")
        self.layout = ttk.Frame(root, padding=12)
        self.layout.grid(row=1, column=0, sticky="nsew")
        self.layout.rowconfigure(0, weight=1)
        rgb = tuple(value // 256 for value in root.winfo_rgb(base))
        shift = -1 if sum(rgb) > 384 else 1
        def tone(amount):
            return "#%02x%02x%02x" % tuple(max(0, min(255, value + shift * amount)) for value in rgb)
        style.configure("Review.TFrame", background=tone(8))
        style.configure("Review.TLabel", background=tone(8))
        self.panels = []
        bodies, action_frames = [], []
        for column, weight in enumerate((1, 2, 1)):
            self.layout.columnconfigure(column, weight=weight, uniform="workspace", minsize=180 * weight)
            shell = tk.Frame(self.layout, background=tone(8) if column == 1 else base,
                             highlightbackground=tone(40) if column == 1 else tone(24),
                             highlightthickness=1, padx=12, pady=12)
            shell.grid(row=0, column=column, sticky="nsew", padx=4)
            shell.columnconfigure(0, weight=1)
            shell.rowconfigure(0, weight=1)
            panel_style = "Review.TFrame" if column == 1 else "TFrame"
            body = ttk.Frame(shell, style=panel_style)
            body.grid(row=0, column=0, sticky="nsew")
            footer = ttk.Frame(shell, style=panel_style)
            footer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
            footer.columnconfigure(0, weight=1)
            self.panels.append(shell)
            bodies.append(body)
            action_frames.append(footer)
        left, middle, right = bodies
        for column, panel in enumerate((left, middle, right)):
            panel.columnconfigure(0, weight=1)
            panel.rowconfigure(1, weight=1)

        self.terminal_status = tk.StringVar(value="Nicht verbunden\n")
        self.terminal_button = self.header(left, "Terminal", self.terminal_status, "Terminal öffnen", self.open_terminal,
                                           "terminal")
        left_space = ttk.Frame(left)
        left_space.grid(row=1, column=0, sticky="nsew")
        output_controls = ttk.Frame(left)
        output_controls.grid(row=2, column=0, sticky="ew", pady=(12, 4))
        output_controls.columnconfigure(0, weight=1)
        ttk.Label(output_controls, text="Ausgabebereich", anchor="center", justify="center").grid(
            row=0, column=0, sticky="ew", pady=(0, 5))
        self.output_mode = tk.StringVar(value=OUTPUT_MODES[core.output_mode])
        self.output_picker = ttk.Combobox(output_controls, textvariable=self.output_mode,
                                          state="readonly", width=20, justify="center")
        self.output_picker.grid(row=1, column=0, sticky="ew")
        self.output_picker.bind("<<ComboboxSelected>>", self.change_output_mode)

        review_header = ttk.Frame(middle, style="Review.TFrame")
        review_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(review_header, text="Review", font=("TkDefaultFont", 11, "bold"),
                  style="Review.TLabel").pack(anchor="w", pady=(0, 8))
        self.direction = tk.StringVar()
        ttk.Label(review_header, textvariable=self.direction, style="Review.TLabel").pack(anchor="w")
        self.editor = ScrollableText(middle, readonly=True, height=16)
        self.review_font = font.nametofont("TkFixedFont", root=root).copy()
        review_size = self.review_font.cget("size")
        if review_size > 8:
            self.review_font.configure(size=review_size - 1)
        elif review_size < -10:
            self.review_font.configure(size=review_size + 1)
        self.editor.text.configure(font=self.review_font, background="#000000",
                                   foreground="#00ff00", insertbackground="#00ff00")
        self.editor.text.tag_configure("control", foreground="#a8c2e0")
        self.editor.text.tag_configure("warning", foreground="#edbd8d")
        self.editor.frame.grid(row=1, column=0, sticky="nsew")
        self.risk_row = ttk.Frame(middle, style="Review.TFrame")
        self.risk_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.risk_enabled = tk.BooleanVar(value=False)
        self.risk_checkbox = ttk.Checkbutton(self.risk_row, text="Mit sh-guard prüfen",
            variable=self.risk_enabled, command=self.change_command_risk)
        self.risk_checkbox.grid(row=0, column=0, sticky="w")
        self.risk_details_button = ttk.Button(self.risk_row, text="Details…", command=self.command_risk_details)
        self.risk_details_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.risk_details_button.bind("<Return>", lambda _: self.risk_details_button.invoke())
        self.risk_row.columnconfigure(0, weight=1)
        self.risk_status = tk.StringVar(value="")
        self.risk_status_label = ttk.Label(self.risk_row, textvariable=self.risk_status,
            style="Review.TLabel", wraplength=360)
        self.risk_status_label.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.editor.text.bind("<Button-1>", self.click_command_risk, add=True)
        self.final_return = tk.BooleanVar(value=core.final_return)
        self.return_row = ttk.Frame(middle, style="Review.TFrame")
        self.return_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.return_checkbox = ttk.Checkbutton(self.return_row, text="Finales Return senden",
                                               variable=self.final_return, command=self.change_return)
        self.return_checkbox.pack(anchor="w")
        self.review_status = tk.StringVar()
        self.binding_value = tk.StringVar(value="")
        self.binding_submission = {}
        self.binding_row = ttk.Frame(middle, style="Review.TFrame")
        self.binding_row.grid(row=4, column=0, sticky="ew")
        self.binding_row.grid_remove()
        self.binding_picker = ttk.Combobox(self.binding_row, textvariable=self.binding_value,
                                           state="readonly")
        ttk.Label(self.binding_row, text="Output gehört zu Submission:",
                  style="Review.TLabel").pack(anchor="w")
        self.binding_picker.pack(fill="x")
        self.binding_picker.bind("<<ComboboxSelected>>", lambda _: setattr(self, "binding_touched", True))
        self.review_status_label = ttk.Label(middle, textvariable=self.review_status, wraplength=360,
                                             style="Review.TLabel")
        self.review_status_label.grid(row=5, column=0, sticky="ew", pady=(8, 4))

        self.ai_status = tk.StringVar(value="Nicht verbunden\nLokaler Testmodus")
        self.header(right, "AI / MCP", self.ai_status,
                    "Verbindung einstellen", self.connection_settings, "ai")
        right_space = ttk.Frame(right)
        right_space.grid(row=1, column=0, sticky="nsew")
        ttk.Label(right_space, text="Command-Blockgröße", anchor="center", justify="center").pack(
            fill="x", pady=(18, 5))
        self.preference = tk.StringVar(value=core.preferences)
        preference = ttk.Combobox(right_space, textvariable=self.preference,
                                  values=BLOCK_SIZES, state="readonly", width=12, justify="center")
        preference.pack(fill="x")
        preference.bind("<<ComboboxSelected>>", lambda _: self.action(lambda: core.set_preferences(self.preference.get())))
        ttk.Separator(right_space, orient="horizontal").pack(fill="x", pady=(20, 12))
        ttk.Label(right_space, text="Chat-Code", anchor="center", justify="center",
                  font=("TkDefaultFont", 10, "bold")).pack(fill="x", pady=(0, 4))
        self.chat_code = tk.StringVar(value="")
        self.chat_code_label = tk.Label(right_space, textvariable=self.chat_code, font=("TkFixedFont", 16, "bold"),
                                        anchor="center", padx=4, pady=5)
        self.chat_code_label.pack(fill="x")
        self.chat_code_status = tk.StringVar(value="Noch nicht verbunden")
        ttk.Label(right_space, textvariable=self.chat_code_status, anchor="center", justify="center",
                  wraplength=230).pack(fill="x", pady=(0, 4))
        self.chat_code_copy = ttk.Button(right_space, text="Chat-Code kopieren", command=self.copy_chat_binding)
        self.chat_code_copy.pack(fill="x")
        self.readiness = tk.Label(right, text="", wraplength=170, justify="center", padx=8, pady=10)
        self.readiness.grid(row=2, column=0, sticky="ew", pady=(12, 4))

        # Equal-height footers keep the existing common action baseline.
        self.button_hand_images = {}
        for name, data in _BUTTON_HAND_PNG.items():
            try:
                self.button_hand_images[name] = tk.PhotoImage(master=root, data=data, format="png")
            except tk.TclError:
                self.button_hand_images[name] = None  # Button text and action remain usable.
        self.from_terminal_button = ttk.Button(action_frames[0], text="Prüfen →",
                                                command=lambda: self.action(core.select_current_output))
        self.from_terminal_button.grid(row=0, column=0, sticky="nsew")
        self.approve_button = ttk.Button(action_frames[1], text="Freigeben", command=self.approve,
                                         image=self.button_hand_images["ok"] or "", compound="left")
        self.approve_button.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.reject_button = ttk.Button(action_frames[1], text="Verwerfen", command=self.reject,
                                        image=self.button_hand_images["stop"] or "", compound="left")
        self.reject_button.grid(row=0, column=1, sticky="nsew")
        self.from_ai_button = ttk.Button(action_frames[2], text="← Prüfen",
                                          command=lambda: self.action(core.review_pending_proposal))
        self.from_ai_button.grid(row=0, column=0, sticky="nsew")
        footer_height = max(button.winfo_reqheight() for button in
                            (self.from_terminal_button, self.approve_button, self.reject_button, self.from_ai_button))
        for footer in action_frames:
            footer.rowconfigure(0, minsize=footer_height)
        for panel in bodies:
            panel.bind("<Configure>", self.fit_panels)

        self.context = tk.Menu(root, tearoff=False)
        self.context.add_command(label="Text bearbeiten…", command=self.edit_dialog)
        self.editor.text.bind("<Button-3>", self.show_context)
        if sys.platform == "darwin":
            self.editor.text.bind("<Button-2>", self.show_context)
        self.editor.text.bind("<Control-e>", lambda _: self.edit_dialog())
        self.editor.text.bind("<Command-e>", lambda _: self.edit_dialog()) if sys.platform == "darwin" else None
        root.protocol("WM_DELETE_WINDOW", self.close)
        # Startup inbox is empty; offline samples require an explicit local action.
        self.tick()

    def header(self, panel, title, status, button_text, command, section):
        frame = self.ttk.Frame(panel)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        image = None
        try:
            image = self.tk.PhotoImage(master=self.root, data=_SECTION_LOGO_PNG[section][self.logo_variant], format="png")
        except self.tk.TclError:
            pass  # Preserve the section title if Tk cannot decode its decorative PNG.
        self.section_images[section] = image
        if image is not None:
            graphic = self.ttk.Label(frame, image=image)
            graphic.grid(row=0, column=0, pady=(0, 4))
            self.section_logo_labels[section] = graphic
        self.ttk.Label(frame, text=title, font=("TkDefaultFont", 11, "bold"),
                       anchor="center", justify="center").grid(row=1, column=0, sticky="ew", pady=(0, 8))
        label = self.ttk.Label(frame, textvariable=status, anchor="center", justify="center", wraplength=180)
        label.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.status_labels.append((label, panel))
        button = self.ttk.Button(frame, text=button_text, command=command)
        button.grid(row=3, column=0, sticky="ew")
        return button

    def fit_panels(self, _event=None):
        for label, panel in self.status_labels:
            label.configure(wraplength=max(120, panel.winfo_width()))
        self.readiness.configure(wraplength=max(120, self.readiness.master.winfo_width() - 16))
        self.review_status_label.configure(wraplength=max(180, self.review_status_label.master.winfo_width()))
        status_height = max(label.winfo_reqheight() for label, _ in self.status_labels)
        for label, _ in self.status_labels:
            label.master.rowconfigure(2, minsize=status_height + 10)

    def action(self, fn):
        try:
            return fn()
        except ProbeError as exc:
            self.messagebox.showerror("Aktion gesperrt", str(exc), parent=self.root)
        except Exception:
            self.messagebox.showerror("Fehler", "Lokale Aktion fehlgeschlagen; keine Inhaltslogs.", parent=self.root)
        finally:
            self.render()

    def open_terminal(self):
        try:
            if self.core.connection == "DISCONNECTED":
                self.core.restart_local()
            self.launcher = self.core.launch_terminal()
            self.render()
        except ProbeError as exc:
            if str(exc) == "xterm_missing_use_manual_command":
                self.show_command()
            else:
                self.messagebox.showerror("Terminal", str(exc), parent=self.root)
        except Exception:
            self.messagebox.showerror("Terminal", "Terminalstart fehlgeschlagen.", parent=self.root)

    def show_command(self):
        window = self.tk.Toplevel(self.root)
        window.title("In einem normalen Terminal ausführen")
        box = ScrollableText(window, readonly=True, height=5)
        box.frame.pack(fill="both", expand=True)
        box.show(((self.core.helper_command(), "plain"),))

    def connection_settings(self):
        if self.connection_dialog and self.connection_dialog.window.winfo_exists():
            self.connection_dialog.window.lift()
            return self.connection_dialog
        self.connection_dialog = self.action(lambda: ConnectionDialog(self))
        return self.connection_dialog

    def copy_chat_binding(self):
        bridge = self.core.bridge_snapshot()
        code, _, can_copy = chat_binding_presentation(bridge)
        require(can_copy and bool(code), "chat_binding_unavailable")
        self.root.clipboard_clear()
        self.root.clipboard_append(code)

    def show_chat_binding(self):
        dialog = self.connection_settings()
        if dialog:
            dialog.run("bridge", dialog.show_binding)

    def change_output_mode(self, _event=None):
        selected = self.output_mode.get()
        self.action(lambda: self.core.set_output_mode(next(key for key, label in OUTPUT_MODES.items() if label == selected)))

    def change_return(self):
        self.action(lambda: self.core.set_final_return(self.final_return.get()))

    def change_command_risk(self):
        self.action(lambda: self.core.request_command_risk(self.risk_enabled.get()))

    def _confirm_command_risk(self):
        self.risk_confirm_pending = False
        if not self.core.risk_requested or self.core.risk_setup_state != "INSTALL_CONFIRM":
            return
        installation = self.core.risk_installation
        release = installation.release
        message = ("sh-guard " + release.version + " installieren?\nEin lokales Analyse-Binary wird von\n"
                   + release.url + "\nnach\n" + str(installation.destination)
                   + "\nheruntergeladen und per SHA-256 geprüft.\nDie Analyse arbeitet lokal/offline. Ihre Freigabe bleibt erforderlich.")
        if sys.platform == "darwin":
            message += "\nmacOS/Gatekeeper kann eine einmalige manuelle Bestätigung verlangen."
        epoch = self.core.risk_setup_epoch
        confirmed = self.messagebox.askyesno("Lokales Analyse-Binary", message, parent=self.root)
        if self.core.risk_setup_epoch == epoch and self.core.risk_setup_state == "INSTALL_CONFIRM":
            self.action(lambda: self.core.confirm_command_risk_install(confirmed))

    def click_command_risk(self, event):
        index = self.editor.text.index("@%d,%d" % (event.x, event.y))
        if any(tag.startswith("risk-sub-") for tag in self.editor.text.tag_names(index)):
            self.command_risk_details()

    def command_risk_details(self):
        if self.risk_dialog and self.risk_dialog.winfo_exists():
            self.risk_dialog.lift()
            return
        window = self.risk_dialog = self.tk.Toplevel(self.root)
        window.title("sh-guard · Analyse und Installation")
        window.geometry("820x600")
        self.risk_details_box = ScrollableText(window, readonly=True, height=18)
        self.risk_details_box.frame.pack(fill="both", expand=True, padx=12, pady=12)
        controls = self.ttk.Frame(window)
        controls.pack(fill="x", padx=12, pady=(0, 12))
        self.ttk.Button(controls, text="Erneut prüfen", command=lambda: self.action(
            lambda: self.core.request_command_risk(True))).pack(side="left")
        self.ttk.Button(controls, text="AITL-sh-guard entfernen…", command=self.remove_command_risk).pack(side="left", padx=8)
        self.ttk.Button(controls, text="Schließen", command=window.destroy).pack(side="right")
        self.risk_render_key = None
        self.render()

    def remove_command_risk(self):
        installation = self.core.risk_installation
        if installation and self.messagebox.askyesno("Lokales Tool entfernen",
                "Nur das geprüfte AITL-Binary entfernen?\n" + str(installation.destination), parent=self.risk_dialog):
            self.action(lambda: self.core.request_command_risk(False))
            self.action(self.core.remove_command_risk)

    def _render_command_risk(self, snapshot, command):
        risk = snapshot.command_risk
        self.risk_enabled.set(self.core.risk_requested or self.core._command_risk.enabled)
        self.risk_checkbox.configure(state="normal" if command or snapshot.review is None else "disabled")
        labels = {"DISABLED": "", "IDLE": "Bereit · kein Command-Review" if not snapshot.review else "Nur AI→Terminal",
                  "CHECKING_TOOL": "sh-guard wird geprüft…", "INSTALL_CONFIRM": "Installation bestätigen…",
                  "INSTALLING": "sh-guard wird installiert…", "SCANNING": "Prüfung läuft…",
                  "WAITING_FOR_REAP": "Vorherige Prüfung wird beendet…",
                  "STALE": "Analyse nicht mehr aktuell", "UNKNOWN": "Analyse unvollständig",
                  "UNAVAILABLE": "Prüfung nicht verfügbar"}
        error = risk.error
        spans = command_risk_spans(snapshot.review.text, risk.result.data) if command and risk.result and risk.result.data else ()
        if risk.state == "RESULT" and spans:
            status = "Risk %d/100 – %s" % (risk.result.data["score"], risk.result.data["level"])
        elif risk.state == "RESULT":
            status, error = "Zuordnung unbekannt", "risk_mapping_unknown"
        else:
            status = labels.get(risk.state, risk.state)
        reasons = {"risk_pin_not_approved": "Artefakt-/SBOM-/Offline-/Lizenz-/Plattformfreigabe fehlt",
                   "risk_platform": "Plattform nicht unterstützt", "risk_setup_busy": "Vorherige Prüfung wird beendet",
                   "risk_mapping_unknown": "keine verlässliche Subcommand-Markierung"}
        if error:
            status += " · " + reasons.get(error, error)
        self.risk_status.set(status)
        if risk.state != "RESULT":
            spans = ()
        key = (snapshot.review, risk, spans)
        if key != self.risk_render_key:
            for tag in self.editor.text.tag_names():
                if tag.startswith("risk-sub-"):
                    self.editor.text.tag_delete(tag)
            self.editor.show_review(snapshot.review.text if snapshot.review else "", spans)
            if self.risk_dialog and self.risk_dialog.winfo_exists():
                def plain(value):
                    if isinstance(value, (dict, MappingProxyType)):
                        return {k: plain(v) for k, v in value.items()}
                    if isinstance(value, tuple):
                        return [plain(v) for v in value]
                    return value
                details = (status or "Reviewer deaktiviert") + "\nStatische Analyse · keine Freigabe oder Sicherheitsgarantie\n"
                if risk.result and risk.result.data:
                    details += ("\nSnapshot: %s · Revision %s · finales Return: %s\nTool: %s\nBinary-SHA-256: %s\nAnalysiert: %s\n"
                        % (risk.result.key.review_id, risk.result.key.revision, risk.result.key.final_return,
                           risk.result.key.tool.version, risk.result.key.tool.sha256, risk.result.analyzed_at))
                    details += "Sitzung: %s\nToolpfad/Target: %s\n" % (risk.result.key.session, risk.result.key.tool.binary)
                    details += "\nGesamtbefund / Parse Confidence / Risk Factors / Subcommands / Pipeline-Taint / MITRE\n"
                    details += json.dumps(plain(risk.result.data), ensure_ascii=False, indent=2)
                    details += "\nquick_decision ist nur ein Upstream-Label, keine AITL-Autorisierung.\n"
                installation = self.core.risk_installation
                if installation:
                    release = installation.release
                    details += ("\nInstallationsziel: " + str(installation.destination) + "\nQuelle: " + release.url
                                + "\nArchiv-SHA-256: " + release.archive_sha256 + "\nBinary-SHA-256: " + release.binary_sha256)
                details += "\nGPL-3.0-only: unabhängige Lizenz-/Distributionsfreigabe erforderlich.\n"
                details += "Commandtext kann kurzzeitig in OS-Prozessargumenten sichtbar sein.\n"
                if sys.platform == "darwin":
                    details += "Gatekeeper: bei nicht eindeutigem Startfehler manuell prüfen; keine automatische Ausnahme/Quarantäneentfernung.\n"
                self.risk_details_box.show(review_fragments(details))
            self.risk_render_key = key
        if risk.state == "INSTALL_CONFIRM" and not self.risk_confirm_pending:
            self.risk_confirm_pending = True
            self.root.after_idle(self._confirm_command_risk)

    def show_context(self, event):
        if self.core.review_state in {"REVIEW", "OVERRIDE_REQUIRED"}:
            self.context.tk_popup(event.x_root, event.y_root)

    def edit_dialog(self):
        snapshot = self.core.snapshot()
        if not snapshot.review or snapshot.review_state not in {"REVIEW", "OVERRIDE_REQUIRED"}:
            return
        review = snapshot.review
        window = self.tk.Toplevel(self.root)
        window.title("Review-Text bearbeiten")
        window.geometry("760x480")
        box = ScrollableText(window, height=16)
        box.frame.pack(fill="both", expand=True, padx=12, pady=12)
        source = review.text.rstrip("\n") if review.direction == "TO_TERMINAL" else review.text
        box.text.insert("1.0", source)

        def apply():
            try:
                self.core.edit_review(review.id, box.text.get("1.0", "end-1c"),
                                      review.final_return if review.direction == "TO_TERMINAL" else False)
            except ProbeError as exc:
                self.messagebox.showerror("Bearbeitung gesperrt", str(exc), parent=window)
                return
            self.render()
            window.destroy()
        self.ttk.Button(window, text="Übernehmen", command=apply).pack(side="left", padx=12, pady=(0, 12))
        self.ttk.Button(window, text="Abbrechen", command=window.destroy).pack(side="right", padx=12, pady=(0, 12))

    def _render_binding_row(self, snapshot, review, active):
        """Explicit Submission-Bindung für Output-Freigaben; keine 'letzter Command'-Heuristik."""
        editable = snapshot.review_state == "REVIEW"
        self.binding_picker.configure(state="readonly" if editable else "disabled")
        if review and self.last_binding_review == review.id and not editable:
            # Preserve the approved label, mapping and visibility even if candidates
            # expire/disappear or the bridge disconnects during delivery.
            return
        candidates = self.core.output_candidates() if snapshot.bridge.state == "CONNECTED" else ()
        visible = bool(review and review.direction == "TO_OUTPUT_SINK" and candidates)
        self.binding_row.grid() if visible else self.binding_row.grid_remove()
        if not visible:
            self.binding_value.set("")
            self.binding_submission = {}
            self.binding_touched = False
            self.last_binding_rows = None
            self.last_binding_review = review.id if review else None
            return
        mapping = {}
        rows = []
        for candidate in candidates:
            submission = candidate["submission_id"]
            display = "Submission " + submission[:10] + "… (" + candidate["text_view"].strip()[:32] + ")"
            mapping[display] = submission
            rows.append(display)
        rows.append("Nicht gebunden (unabhängige Beobachtung)")
        mapping[rows[-1]] = None
        stable = tuple(rows)
        default = next((label for label, value in mapping.items()
                        if value == self.core.last_proposal_id), rows[-1])
        review_gone = self.last_binding_review != review.id
        if stable != self.last_binding_rows or review_gone:
            self.binding_submission = mapping
            self.binding_picker.configure(values=rows)
            previous = self.binding_value.get()
            if previous in mapping and self.binding_touched and not review_gone:
                self.binding_value.set(previous)
            else:
                self.binding_value.set(default)
                self.binding_touched = False
        elif self.binding_value.get() not in self.binding_submission:
            self.binding_submission = mapping
        self.last_binding_rows = stable
        self.last_binding_review = review.id

    def approve(self):
        def run():
            snapshot = self.core.snapshot()
            require(snapshot.review is not None, "no_review")
            if snapshot.review_state == "DELIVERY_UNCERTAIN":
                self.core.retry_output(snapshot.review.id)
                return
            override = snapshot.review_state == "OVERRIDE_REQUIRED"
            if override and not self.messagebox.askyesno("Prompt unbekannt", "Shell-Prompt nicht erkannt. Trotzdem genau diesen Review an diese Sitzung senden?", parent=self.root):
                return
            # The display is read-only and bound to this exact frozen review.
            label = self.binding_value.get()
            require(label in self.binding_submission if self.binding_submission else label == "",
                    "unknown_output_binding")
            submission = self.binding_submission[label] if self.binding_submission else None
            self.core.approve(snapshot.review.id, override=override, submission_id=submission,
                              explicit_binding=bool(self.binding_submission))
        self.action(run)

    def reject(self):
        self.action(lambda: self.core.reject(self.core.review.id) if self.core.review else None)

    def render(self):
        snapshot = self.core.snapshot()
        self.terminal_status.set({"CONNECTED": "Terminal verbunden\nBash · lokale Sitzung",
                                  "WAITING_FOR_TERMINAL": "Nicht verbunden\nTerminal kann geöffnet werden",
                                  "STARTING_SHELL": "Terminal verbindet\nShell startet",
                                  "DISCONNECTED": "Terminal getrennt\nErneutes Öffnen möglich"}[snapshot.connection])
        connected = snapshot.connection == "CONNECTED"
        if connected:
            self.terminal_button.grid_remove()
        else:
            self.terminal_button.grid()
        self.terminal_button.configure(state="disabled" if snapshot.connection == "STARTING_SHELL" else "normal")
        active = snapshot.review_state in {"REVIEW", "OVERRIDE_REQUIRED", "DELIVERY_UNCERTAIN"}
        busy = snapshot.review_state in {"CHECKING", "TRANSFERRING", "SENDING", "DELIVERY_UNCERTAIN"}
        bridge = snapshot.bridge
        self.ai_status.set(local_setup_summary(self.core) if self.core.local_setup_state != "IDLE" else connection_summary(bridge))
        chat_code, chat_status, can_copy = chat_binding_presentation(bridge)
        self.chat_code.set(chat_code)
        if self.core.chat_binding_notice:
            chat_status = self.core.chat_binding_notice
        elif bridge.chat_binding_error == "chat_binding_generation_conflict":
            chat_status = "Code wurde bereits geändert · Status wird aktualisiert"
        self.chat_code_status.set(chat_status)
        self.chat_code_copy.configure(state="normal" if can_copy else "disabled")
        self.from_terminal_button.configure(state="normal" if connected and not busy else "disabled")
        self.from_ai_button.configure(state="normal" if connected and snapshot.inbox and not busy else "disabled")
        ready = bool(snapshot.inbox)
        self.readiness.configure(text="Command-Vorschlag vorhanden" if ready else "Kein Command-Vorschlag",
                                 background="#dcefe1" if ready else "#f6e4d0",
                                 foreground="#285739" if ready else "#75451b")
        modes = [label for key, label in OUTPUT_MODES.items() if key != "SINCE_LAST" or snapshot.last_transfer_offset is not None]
        self.output_picker.configure(values=modes)
        self.output_mode.set(OUTPUT_MODES[snapshot.output_mode])
        self.preference.set(snapshot.preferences)
        self.final_return.set(snapshot.final_return)
        review = snapshot.review
        command = review is not None and review.direction == "TO_TERMINAL"
        if command:
            self.return_row.grid()
            self.final_return.set(review.final_return)
            self.return_checkbox.configure(state="normal" if active else "disabled")
        else:
            self.return_row.grid_remove()
        self._render_binding_row(snapshot, review, active)
        self.direction.set("AI → Terminal" if command else "Terminal → AI" if review else "")
        if review and review.edited:
            self.direction.set(self.direction.get() + " · lokal bearbeitet")
        self.approve_button.configure(text="← Freigeben ins Terminal" if command else "Freigeben an AI →" if review else "Freigeben",
                                       state="normal" if active and not snapshot.paused else "disabled")
        if snapshot.review_state == "DELIVERY_UNCERTAIN":
            self.approve_button.configure(text="Zustellung erneut versuchen →",
                                          state="normal" if bridge.state == "CONNECTED" and not snapshot.paused else "disabled")
        self.reject_button.configure(state="normal" if active else "disabled")
        messages = {"EMPTY": "", "REVIEW": "", "CHECKING": "Übertragung wird geprüft…",
                    "OVERRIDE_REQUIRED": "Prompt nicht erkannt · zusätzliche Bestätigung erforderlich",
                    "TRANSFERRED": "Text übertragen", "TRANSFER_UNCERTAIN": "Übertragung ungewiss · keine Wiederholung",
                    "ACKNOWLEDGED_LOCAL": "Output lokal freigegeben · keine AI-Verbindung",
                    "SENDING": "Freigegebenen Output zustellen…", "ACKNOWLEDGED": "Output an Bridge zugestellt",
                    "DELIVERY_UNCERTAIN": "Zustellung ungewiss · nur derselbe freigegebene Output darf erneut zugestellt werden",
                    "REJECTED": "Verworfen", "STALE": "Review nicht mehr gültig"}
        message = messages.get(snapshot.review_state, snapshot.review_state)
        if command and "\n" in review.text.rstrip("\n"):
            message += " | Interne Returns können bereits Befehle ausführen"
        if review and review.lost_prefix:
            message += " | Früherer Output fehlt"
        if snapshot.last_error:
            message += " | " + snapshot.last_error
        if bridge.last_error:
            message += " | " + bridge.last_error
        if bridge.certificate_warning:
            message += " | Bridge-Zertifikat läuft in weniger als 30 Tagen ab"
        self.review_status.set(message)
        self._render_command_risk(snapshot, command)
        self.last_snapshot = snapshot
        self.fit_panels()
        if self.connection_dialog and self.connection_dialog.window.winfo_exists():
            self.connection_dialog.render()

    def tick(self):
        self.core.pump()
        if self.launcher and self.launcher.poll() is not None:
            result = self.launcher.returncode
            self.launcher = None
            if result != 0:
                self.messagebox.showerror("Terminal", "Terminalstart fehlgeschlagen. Der manuelle Aufruf wird angezeigt.", parent=self.root)
                self.show_command()
        self.render()
        self.root.after(40, self.tick)

    def close(self):
        self.core.close()
        self.root.destroy()


def run_gui():
    import tkinter as tk  # Only GUI mode imports Tk or requires a display.
    core = AppCore(PreferenceStore(preference_directory()), ConnectionStore(preference_directory()))
    try:
        root = tk.Tk()
        core.start_local(os.getcwd())
        ReviewUI(root, core)
        root.mainloop()
    finally:
        core.close()



def outer_test_child(fd, directory):
    os.setsid()
    fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    for target in (0, 1, 2):
        os.dup2(fd, target)
    if fd > 2:
        os.close(fd)
    run_terminal_helper(directory)


class IntegrationFixture:
    """Real helper process, outer terminal PTY, inner shell PTY, Unix IPC."""
    def __init__(self, workdir):
        self.core = AppCore()
        endpoint = self.core.start_local(workdir)
        self.outer, slave = os.openpty()
        termios.tcsetwinsize(slave, (24, 80))
        self.original_attrs = termios.tcgetattr(slave)
        try:
            self.proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()),
                                        "--_outer-test-child", str(slave), endpoint],
                                        pass_fds=(slave,), close_fds=True,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        finally:
            os.close(slave)
        os.set_blocking(self.outer, False)
        self.transcript = bytearray()
        try:
            self.wait(lambda: self.core.connection == "CONNECTED" and b"AITL_P0$ " in self.core.output.data)
        except BaseException:
            self.close()
            raise

    def pump(self):
        self.core.pump()
        for _ in range(8):
            try:
                raw = os.read(self.outer, 32768)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not raw:
                break
            self.transcript.extend(raw)
            require(len(self.transcript) <= 4 * 1024 * 1024, "test_transcript_limit")

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            select.select([self.outer], [], [], 0.01)
        raise ProbeError("integration_timeout")

    def manual(self, text):
        raw = text.encode() if isinstance(text, str) else text
        require(len(raw) <= 4096, "test_manual_limit")
        os.write(self.outer, raw)

    def approve_command(self, text, final_return=True):
        item = self.core.review_command(text, final_return)
        self.core.approve(item.id)
        self.wait(lambda: self.core.review_state in {"TRANSFERRED", "OVERRIDE_REQUIRED", "TRANSFER_UNCERTAIN"})
        return item

    def close(self):
        self.core.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        os.close(self.outer)


class LocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aitl-integration-")
        self.addCleanup(self.tmp.cleanup)
        self.fixture = IntegrationFixture(self.tmp.name)
        self.addCleanup(self.fixture.close)
        self.core = self.fixture.core

    def test_full_round_trip_and_single_approval(self):
        core, fixture = self.core, self.fixture
        self.assertFalse(core.endpoint.capfile.exists())
        proposal = core.add_proposal(": > round-trip-marker; " + marker_command("ROUND_TRIP"))
        self.assertIsNone(core.snapshot().review)
        self.assertFalse(Path(self.tmp.name, "round-trip-marker").exists())
        item = core.review_proposal(proposal, True)
        self.assertEqual(core.snapshot().review.payload, item.text.encode())
        core.approve(item.id)
        fixture.wait(lambda: core.review_state == "TRANSFERRED" and b"\r\nROUND_TRIP\r\n" in core.output.data)
        self.assertTrue(Path(self.tmp.name, "round-trip-marker").exists())
        with self.assertRaises(ProbeError):
            core.approve(item.id)
        output = core.select_output(50, since=core.offsets[item.id])
        self.assertFalse(core.snapshot().approved_outputs)
        self.assertIn("ROUND_TRIP", output.text)
        core.approve(output.id)
        self.assertEqual(core.snapshot().approved_outputs, (output,))
        self.assertEqual(core.review_state, "ACKNOWLEDGED_LOCAL")

    def test_edit_inbox_pause_preferences_and_stale_review(self):
        proposal = self.core.add_proposal(": > blocked-marker")
        item = self.core.review_proposal(proposal)
        next_proposal = self.core.add_proposal(": > other-marker")
        self.assertEqual(self.core.snapshot().review, item)
        updated = self.core.edit_review(item.id, marker_command("EDITED"), True)
        self.assertNotEqual(updated.id, item.id)
        with self.assertRaises(ProbeError):
            self.core.approve(item.id)
        self.core.set_paused(True)
        with self.assertRaises(ProbeError):
            self.core.approve(updated.id)
        self.core.set_preferences("LARGE")
        self.assertEqual(self.core.snapshot().preferences, "LARGE")
        self.core.set_paused(False)
        self.core.reject(updated.id)
        with self.assertRaises(ProbeError):
            self.core.approve(updated.id)
        self.core.remove_proposal(next_proposal)
        self.assertFalse(Path(self.tmp.name, "blocked-marker").exists())

    def test_draft_guard_snapshot_immutable_and_return_visible(self):
        item = self.core.review_command("printf '€'", True)
        self.core.verify_draft(item.id, "printf '€'", True)
        self.assertEqual(json.loads(item.presentation).encode(), item.payload)
        with self.assertRaises(ProbeError):
            self.core.verify_draft(item.id, "printf 'changed'", True)
        with self.assertRaises(ProbeError):
            self.core.verify_draft(item.id, "printf '€'", False)
        with self.assertRaises(AttributeError):
            self.core.snapshot().review.text = "changed"
        self.assertEqual(self.core.review_state, "REVIEW")

    def test_large_relay_output_is_framed_and_utf8_preserved(self):
        self.fixture.manual("printf '%100000s' ''; printf '€\\nLARGE_RELAY_DONE\\n'\n")
        self.fixture.wait(lambda: b"\r\nLARGE_RELAY_DONE\r\n" in self.core.output.data)
        self.assertGreater(self.core.output.end, 100000)
        self.assertIn("€".encode(), self.core.output.data)
        self.assertEqual(self.core.connection, "CONNECTED")

    def test_invalid_session_cannot_be_overridden(self):
        item = self.core.review_command(": > wrong-session-marker", True)
        self.core.channel.send({"type": "transfer", "id": item.id, "session": "wrong-session",
                                "text": item.text, "return": True, "override": True})
        self.fixture.wait(lambda: self.core.connection == "DISCONNECTED")
        self.assertEqual(self.core.review_state, "STALE")
        self.assertFalse(Path(self.tmp.name, "wrong-session-marker").exists())

    def test_gap_in_output_fails_closed(self):
        item = self.core.review_command(": > gap-marker", True)
        self.core.channel.close()
        self.core.channel = None
        with self.assertRaises(ProbeError):
            self.core._event({"type": "output", "start": self.core.output.end + 1, "data": "eA=="})
        self.core._disconnect()
        with self.assertRaises(ProbeError):
            self.core.approve(item.id)
        self.assertEqual(self.core.review_state, "STALE")

    def test_manual_relay_password_and_output_disclosure(self):
        self.fixture.manual("IFS= read -r -s -p 'LOCAL_PASSWORD: ' pw; printf '\\nPW_DONE\\n'; unset pw\n")
        self.fixture.wait(lambda: self.core.output.data.endswith(b"LOCAL_PASSWORD: "))
        secret = ("synthetic_" + uuid.uuid4().hex).encode()
        item = self.core.review_command("program-input", True)
        self.core.approve(item.id)
        self.fixture.wait(lambda: self.core.review_state == "OVERRIDE_REQUIRED")
        self.core.reject(item.id)
        self.fixture.manual(secret + b"\n")
        self.fixture.wait(lambda: b"\r\nPW_DONE\r\n" in self.core.output.data)
        self.assertNotIn(secret, self.core.output.data)
        self.assertNotIn(secret, self.fixture.transcript)
        selection = self.core.select_output()
        self.assertNotIn(secret.decode(), selection.text)
        self.assertFalse(self.core.snapshot().approved_outputs)

    def test_unknown_prompt_override_and_cancel(self):
        self.fixture.manual("IFS= read -r -p 'UNKNOWN: ' answer; printf '\\nREAD_DONE\\n'\n")
        self.fixture.wait(lambda: self.core.output.data.endswith(b"UNKNOWN: "))
        item = self.core.review_command("answer", True)
        self.core.approve(item.id)
        self.fixture.wait(lambda: self.core.review_state == "OVERRIDE_REQUIRED")
        self.assertNotIn(b"\r\nREAD_DONE\r\n", self.core.output.data)
        self.core.approve(item.id, override=True)
        self.fixture.wait(lambda: self.core.review_state == "TRANSFERRED" and b"\r\nREAD_DONE\r\n" in self.core.output.data)

    def test_multiline_and_no_final_enter(self):
        item = self.fixture.approve_command(": > first-line\n: > last-line", False)
        self.fixture.wait(lambda: Path(self.tmp.name, "first-line").exists())
        self.assertFalse(Path(self.tmp.name, "last-line").exists())
        self.assertFalse(item.payload.endswith(b"\n"))
        self.fixture.manual("\n")
        self.fixture.wait(lambda: Path(self.tmp.name, "last-line").exists())

    def test_resize_ctrl_c_and_exit_restore_outer_terminal(self):
        termios.tcsetwinsize(self.fixture.outer, (39, 123))
        os.kill(self.fixture.proc.pid, signal.SIGWINCH)
        self.fixture.manual("stty size; printf '\\nSIZE_LOCAL\\n'\n")
        self.fixture.wait(lambda: b"\r\nSIZE_LOCAL\r\n" in self.core.output.data)
        self.assertRegex(bytes(self.core.output.data), rb"(?:^|[\r\n])39 123\r\n")
        # Echo of the typed line is not proof the foreground job has started.
        self.fixture.manual("bash -c 'printf \"\\nFOREGROUND_READY\\n\"; exec sleep 30'\n")
        self.fixture.wait(lambda: b"\r\nFOREGROUND_READY\r\n" in self.core.output.data)
        self.fixture.manual(b"\x03")
        self.fixture.manual(marker_command("AFTER_CTRL_C") + "\n")
        self.fixture.wait(lambda: b"\r\nAFTER_CTRL_C\r\n" in self.core.output.data)
        self.fixture.manual("exit\n")
        self.fixture.wait(lambda: self.fixture.proc.poll() is not None)
        self.assertEqual(self.fixture.proc.returncode, 0)
        self.assertEqual(termios.tcgetattr(self.fixture.outer), self.fixture.original_attrs)

    def test_disconnect_invalidates_review_manual_terminal_survives(self):
        item = self.core.review_command(": > stale-marker", True)
        self.core.close()
        with self.assertRaises(ProbeError):
            self.core.approve(item.id)
        self.fixture.manual(marker_command("DETACHED_MANUAL") + "\n")
        self.fixture.wait(lambda: b"\r\nDETACHED_MANUAL\r\n" in self.fixture.transcript)
        self.assertFalse(Path(self.tmp.name, "stale-marker").exists())
        self.assertEqual(self.core.output.end, self.core.snapshot().output_end)
        self.assertFalse(self.core.output.data)

    def test_duplicate_ipc_request_does_not_repeat_execution(self):
        item = self.fixture.approve_command("printf x >> ipc-once; " + marker_command("IPC_ONCE"))
        self.fixture.wait(lambda: b"\r\nIPC_ONCE\r\n" in self.core.output.data)
        self.core.channel.send({"type": "transfer", "id": item.id, "session": item.session,
                                "text": item.text, "return": True, "override": True})
        self.fixture.wait(lambda: self.core.connection == "DISCONNECTED")
        self.assertEqual(Path(self.tmp.name, "ipc-once").read_bytes(), b"x")

    def test_output_selection_frozen_sanitized_and_editable(self):
        self.fixture.manual("printf '\\033]52;c;FAKE_CLIPBOARD\\007\\033[31mVISIBLE\\033[0m\\n'; printf '\\nSAN_DONE\\n'\n")
        self.fixture.wait(lambda: b"\r\nSAN_DONE\r\n" in self.core.output.data)
        output = self.core.select_output()
        self.assertNotIn("\x1b", output.text)
        self.assertIn("VISIBLE", output.text)
        self.fixture.manual(marker_command("LATER_OUTPUT") + "\n")
        self.fixture.wait(lambda: b"\r\nLATER_OUTPUT\r\n" in self.core.output.data)
        self.assertEqual(self.core.snapshot().review, output)
        edited = self.core.edit_review(output.id, "explicitly selected text")
        self.core.approve(edited.id)
        self.assertEqual(self.core.snapshot().approved_outputs[-1].payload, b"explicitly selected text")

    def test_one_proposal_slot_consumed_without_automatic_review(self):
        proposal = self.core.add_proposal(marker_command("ONE_SLOT"))
        with self.assertRaises(ProbeError):
            self.core.add_proposal(marker_command("SECOND_SLOT"))
        self.assertIsNone(self.core.snapshot().review)
        self.assertEqual(len(self.core.snapshot().inbox), 1)
        review = self.core.review_pending_proposal()
        self.assertFalse(self.core.snapshot().inbox)
        self.assertTrue(review.final_return)
        self.assertEqual(review.text, marker_command("ONE_SLOT") + "\n")
        with self.assertRaises(ProbeError):
            self.core.review_pending_proposal()
        with self.assertRaises(ProbeError):
            self.core.review_proposal(proposal)

    def test_invalid_proposal_is_not_consumed(self):
        proposal = self.core.add_proposal("printf a\x1b")
        with self.assertRaises(ProbeError):
            self.core.review_pending_proposal()
        self.assertEqual(self.core.snapshot().inbox[0][0], proposal)
        self.assertIsNone(self.core.review)

    def test_return_preference_sticky_and_output_does_not_change_it(self):
        review = self.core.review_command(": > preference-marker")
        self.assertTrue(review.final_return)
        changed = self.core.set_final_return(False)
        self.assertNotEqual(review.id, changed.id)
        self.assertFalse(changed.payload.endswith(b"\n"))
        with self.assertRaises(ProbeError):
            self.core.approve(review.id)
        self.core.reject(changed.id)
        output = self.core.select_current_output()
        self.assertFalse(output.final_return)
        next_review = self.core.review_command(marker_command("RETURN_STAYS_OFF"))
        self.assertFalse(next_review.final_return)
        self.assertFalse(self.core.snapshot().final_return)
        self.assertFalse(Path(self.tmp.name, "preference-marker").exists())

    def test_output_default_last_twenty_then_since_confirmed_transfer(self):
        self.assertEqual(self.core.snapshot().output_mode, "LAST_20")
        with self.assertRaises(ProbeError):
            self.core.set_output_mode("SINCE_LAST")
        self.fixture.manual("for i in {0..29}; do echo DEFAULT_RANGE_$i; done\n")
        def default_output_complete():
            data = bytes(self.core.output.data)
            marker = data.rfind(b"DEFAULT_RANGE_29\r\n")
            return marker >= 0 and data.rfind(b"AITL_P0$ ") > marker
        self.fixture.wait(default_output_complete)
        before = self.core.select_current_output()
        default_rows = before.text.splitlines()
        self.assertEqual(len(default_rows), 20)
        self.assertIn("DEFAULT_RANGE_11", before.text)
        self.assertIn("DEFAULT_RANGE_29", before.text)
        self.assertNotIn("DEFAULT_RANGE_10", before.text)
        review = self.core.review_command(marker_command("RANGE_DEFAULT"))
        self.assertEqual(self.core.output_mode, "LAST_20")
        self.core.approve(review.id)
        self.fixture.wait(lambda: self.core.review_state == "TRANSFERRED" and b"\r\nRANGE_DEFAULT\r\n" in self.core.output.data)
        self.assertEqual(self.core.snapshot().output_mode, "SINCE_LAST")
        self.assertEqual(self.core.snapshot().last_transfer_offset, self.core.offsets[review.id])
        self.assertIn("RANGE_DEFAULT", self.core.select_current_output().text)
        self.core.set_output_mode("LAST_1")
        one_line = self.core.select_current_output()
        self.assertEqual(len(one_line.text.splitlines()), 1)
        self.fixture.manual("printf '\\nMANUAL_RANGE\\n'\n")
        self.fixture.wait(lambda: b"\r\nMANUAL_RANGE\r\n" in self.core.output.data)
        self.assertEqual(self.core.output_mode, "LAST_1")
        self.core.set_output_mode("SINCE_LAST")
        self.assertIn("MANUAL_RANGE", self.core.select_current_output().text)

    def test_since_transfer_is_not_silently_limited_to_500_lines(self):
        review = self.fixture.approve_command("for ((i=0;i<600;i++)); do printf 'RANGE_ROW_%s\\n' \"$i\"; done; " + marker_command("RANGE_ALL_DONE"))
        self.fixture.wait(lambda: b"\r\nRANGE_ALL_DONE\r\n" in self.core.output.data)
        selected = self.core.select_current_output()
        self.assertIn("RANGE_ROW_0\n", selected.text)
        self.assertIn("RANGE_ROW_599\n", selected.text)
        self.assertEqual(self.core.last_transfer_offset, self.core.offsets[review.id])


class PreferenceAndPresentationTests(unittest.TestCase):
    def test_first_start_and_persisted_single_return_off(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory) / "config")
            core = AppCore(store)
            self.assertTrue(core.snapshot().final_return)
            self.assertEqual(core.preferences, "SMALL")
            core.set_final_return(False)
            core.set_preferences("SINGLE")
            reloaded = AppCore(PreferenceStore(Path(directory) / "config"))
            self.assertFalse(reloaded.snapshot().final_return)
            self.assertEqual(reloaded.snapshot().preferences, "SINGLE")
            self.assertIsNone(reloaded.snapshot().review)
            config = Path(directory) / "config" / "preferences.json"
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o700)

    def test_corrupt_or_unsafe_preferences_do_not_reset_return_to_on(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory) / "config")
            store.save(False, "SINGLE")
            target = store.directory / "preferences.json"
            target.write_bytes(b'{"bad":true}')
            with self.assertRaises(ProbeError):
                AppCore(store)
            target.write_text('{"version":1,"final_return":false,"block_size":"SINGLE"}')
            target.chmod(0o644)
            with self.assertRaises(ProbeError):
                AppCore(store)

    def test_symlink_preferences_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.mkdir(mode=0o700)
            outside = Path(directory) / "outside"
            outside.write_bytes(b"synthetic_untouched")
            (config / "preferences.json").symlink_to(outside)
            with self.assertRaises(ProbeError):
                PreferenceStore(config).save(False, "SINGLE")
            self.assertEqual(outside.read_bytes(), b"synthetic_untouched")

    def test_write_failure_keeps_previous_preference_and_removes_temp_file(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            store = PreferenceStore(Path(directory) / "config")
            store.save(True, "SMALL")
            core = AppCore(store)
            with patch.object(os, "replace", side_effect=OSError()):
                with self.assertRaises(ProbeError):
                    core.set_final_return(False)
            self.assertTrue(core.snapshot().final_return)
            self.assertEqual(store.load(), (True, "SMALL"))
            self.assertEqual([p.name for p in store.directory.iterdir()], ["preferences.json"])

    def test_tagged_projection_does_not_change_payload_or_soft_wrap_lines(self):
        text = "printf '€' " + "x" * 3000 + "\n"
        review = LocalReview("id", 1, "TO_TERMINAL", "session", text, text.encode(), True)
        projection = review_fragments(review.text)
        self.assertIn(("↵\n", "control"), projection)
        self.assertTrue(any(tag == "unicode" for _, tag in projection))
        self.assertEqual(review.payload, text.encode())
        self.assertEqual(review.payload.count(b"\n"), 1)
        self.assertIn(("⟦U+202E⟧", "control"), review_fragments("\u202e"))
        literal_arrow = review_fragments("↵")
        self.assertEqual(literal_arrow, (("⟦U+21B5⟧", "control"),))


class ProtocolTests(unittest.TestCase):
    def test_bad_auth_cannot_attach_and_capability_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            core = AppCore()
            path = Path(core.start_local(directory))
            try:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((path / "capability").stat().st_mode), 0o600)
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(path / "controller.sock"))
                core.pump()
                receive_blocking(sock)
                sock.sendall(packet({"type": "authenticate", "nonce": "0" * 64, "proof": "0" * 64}))
                core.pump()
                self.assertIsNone(core.channel)
                self.assertTrue((path / "capability").exists())
                sock.close()
            finally:
                core.close()

    def test_fragmentation_schema_size_and_backpressure(self):
        a, b = socket.socketpair()
        channel = Channel(a)
        try:
            raw = packet({"type": "test", "value": "€"})
            b.sendall(raw[:3])
            self.assertEqual(channel.receive(), [])
            b.sendall(raw[3:])
            self.assertEqual(channel.receive(), [{"type": "test", "value": "€"}])
            b.sendall(struct.pack("!I", FRAME_LIMIT + 1))
            with self.assertRaises(ProbeError):
                channel.receive()
            with self.assertRaises(ProbeError):
                decode_packet(b'{"x":1,"x":2}')
            with self.assertRaises(ProbeError):
                decode_packet(b'{"x":NaN}')
            channel.outgoing = bytearray(QUEUE_LIMIT)
            with self.assertRaises(ProbeError):
                channel.send({"type": "anything"})
        finally:
            channel.close()
            b.close()

    def test_frame_timeout_and_eof_after_complete_message(self):
        a, b = socket.socketpair()
        channel = Channel(a)
        try:
            b.sendall(b'\x00')
            channel.receive()
            channel.partial_since = time.monotonic() - 6
            with self.assertRaises(ProbeError):
                channel.receive()
        finally:
            channel.close()
            b.close()
        a, b = socket.socketpair()
        channel = Channel(a)
        b.sendall(packet({"type": "last"}))
        b.close()
        try:
            self.assertEqual(channel.receive(), [{"type": "last"}])
            with self.assertRaises(ProbeError):
                channel.receive()
        finally:
            channel.close()

    def test_sanitizer_bounds_and_json_review_roundtrip(self):
        self.assertEqual(sanitized_output(b'a\x1b[31mb\x1b[0m\r\nc'), 'ab\nc')
        self.assertNotIn("\x1b", sanitized_output(b'\x1b]' + b'x' * 5000))
        self.assertIn("[U+202E]", sanitized_output("a\u202e".encode()))
        text = "literal \\n and €\n"
        review = LocalReview("a", 1, "TO_TERMINAL", "s", text, text.encode(), True)
        self.assertEqual(json.loads(review.presentation).encode(), review.payload)
        with self.assertRaises(ProbeError):
            sanitized_output(b'x' * (OUTPUT_LIMIT + 1))

    def test_import_does_not_load_tk_or_start_processes(self):
        code = 'import sys; import admin_in_the_loop; assert "tkinter" not in sys.modules'
        result = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parent),
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)


class SafeResult(unittest.TextTestResult):
    """Never print assertion values/tracebacks which may contain payloads."""
    def _exc_info_to_string(self, err, test):
        reason = str(err[1]) if isinstance(err[1], ProbeError) else ""
        suffix = " / " + reason if re.fullmatch("[a-z_]+", reason) else ""
        return "Test failed: " + err[0].__name__ + suffix + " (content omitted)\n"


def run_all_tests():
    suite = unittest.TestSuite()
    for name, function in TESTS:
        def wrapped(fn=function):
            with tempfile.TemporaryDirectory(prefix="aitl-p0-regression-") as directory:
                fn(directory)
        suite.addTest(unittest.FunctionTestCase(wrapped, description=name))
    loader = unittest.TestLoader()
    suite.addTests(loader.loadTestsFromTestCase(LocalIntegrationTests))
    suite.addTests(loader.loadTestsFromTestCase(ProtocolTests))
    suite.addTests(loader.loadTestsFromTestCase(PreferenceAndPresentationTests))
    result = unittest.TextTestRunner(verbosity=2, resultclass=SafeResult).run(suite)
    return 0 if result.wasSuccessful() else 1


## BEGIN AITL EMBEDDED BRIDGE SOURCE
## This source block is loaded alone by --bridge, before controller initialization.
import argparse
import base64
import binascii
import hashlib
import hmac
import http.client
import http.server
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

BRIDGE_BODY_LIMIT = 128 * 1024
BRIDGE_RETENTION = 24 * 3600
BRIDGE_OUTPUT_LIMIT = 64 * 1024
BRIDGE_BLOCK_SIZES = ("SINGLE", "SMALL", "MEDIUM", "LARGE")
BRIDGE_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
BRIDGE_SERVER_INFO = {"name": "admin-in-the-loop", "version": "0.9.0"}
BRIDGE_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BRIDGE_CHAT_ALPHABET = "23456789ACFHJKMNPQRTVWXY"
BRIDGE_CHAT_LIFETIME = 24 * 3600
BRIDGE_CHAT_BACKOFF = (1, 2, 4, 8, 16, 32, 60, 60)
BRIDGE_CHAT_PRINCIPAL_FAILURES = 12
BRIDGE_CHAT_BINDING_FAILURES = 30
BRIDGE_CHAT_WINDOW = 900
BRIDGE_CHAT_LOCKOUT = 900
BRIDGE_CHAT_CODE_SCHEMA = {"type": "string", "minLength": 8, "maxLength": 64,
                           "pattern": "^[A-Za-z0-9 -]+$",
                           "description": "The short human-shared chat code, formatted XXXX-XXXX. Never guess it."}


def new_chat_code(except_code=None):
    while True:
        value = "".join(secrets.choice(BRIDGE_CHAT_ALPHABET) for _ in range(8))
        if value != except_code:
            return value


def normalize_chat_code(value):
    if type(value) is not str or len(value) > 64:
        return ""
    if any(ord(char) > 127 or char not in BRIDGE_CHAT_ALPHABET + BRIDGE_CHAT_ALPHABET.lower() + " -"
           for char in value):
        return ""
    normalized = value.replace("-", "").replace(" ", "").upper()
    return normalized if len(normalized) == 8 and all(char in BRIDGE_CHAT_ALPHABET for char in normalized) else ""


class BridgeError(Exception):
    """Only fixed public codes, never payload/credential/exception text."""
    def __init__(self, code, status=400, rpc=None, data=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.rpc, self.data = rpc, data


class BridgeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Stopped: invalid_cli_arguments (content omitted).\n")


def check(condition, code="invalid_schema", status=400):
    if not condition:
        raise BridgeError(code, status)


def exact(value, required, optional=()):
    check(type(value) is dict and set(required) <= set(value)
          and set(value) <= set(required) | set(optional))


def text(value, limit, pattern=None):
    check(type(value) is str and len(value) <= limit)
    try:
        check(len(value.encode("utf-8")) <= limit)
    except UnicodeError:
        raise BridgeError("invalid_utf8") from None
    if pattern:
        check(re.fullmatch(pattern, value) is not None)
    return value


def opaque(value):
    return text(value, 64, r"[a-zA-Z0-9_-]{16,64}")


def parse(raw):
    check(0 < len(raw) <= BRIDGE_BODY_LIMIT, "body_limit", 413)
    def pairs(items):
        result = {}
        for key, value in items:
            check(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(BridgeError("invalid_json")))
    except BridgeError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalid_json") from None
    check(type(result) is dict)
    return result


def encoded(value):
    try:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise BridgeError("invalid_json") from None
    check(len(raw) <= BRIDGE_BODY_LIMIT, "body_limit", 413)
    return raw


def protected_directory(path):
    path = Path(path).absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    check(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
          and stat.S_IMODE(info.st_mode) == 0o700, "unsafe_state_directory")
    return path


def private_read(path, limit=BRIDGE_BODY_LIMIT):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                  and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                  "unsafe_state_file")
            raw = source.read(limit + 1)
        check(len(raw) <= limit, "state_limit")
        return raw
    except OSError:
        raise BridgeError("state_read_failure") from None


def private_write(path, raw):
    path = Path(path)
    if path.exists() or path.is_symlink():
        private_read(path, max(BRIDGE_BODY_LIMIT, len(raw)))
    temp = path.parent / (".write-" + uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        raise BridgeError("state_write_failure") from None
    finally:
        temp.unlink(missing_ok=True)


class BridgeStore:
    """Bounded SQLite transport state. No human approval or terminal state here."""
    def __init__(self, directory, conversation, clock=time.time):
        opaque(conversation)
        self.directory = protected_directory(directory)
        self.clock, self.conversation = clock, conversation
        self.instance = uuid.uuid4().hex  # Restart changes the controller generation.
        self.on_new_output = None  # Optional service hook; the store stays usable without one.
        self.lock = threading.RLock()
        path = self.directory / "bridge.sqlite3"
        if path.exists() or path.is_symlink():
            private_read(path, 64 * 1024 * 1024)
        # One shared connection; every access takes self.lock because requests run on threads.
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.executescript("""
          PRAGMA journal_mode=DELETE;
          PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL,
            role TEXT NOT NULL, conversation TEXT NOT NULL, state TEXT NOT NULL, deadline REAL);
          CREATE TABLE IF NOT EXISTS proposals(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, request TEXT UNIQUE NOT NULL, text TEXT NOT NULL,
            expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS outputs(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT UNIQUE NOT NULL, idem TEXT UNIQUE NOT NULL, hash TEXT NOT NULL,
            submission TEXT, text TEXT NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS cursors(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
            kind TEXT NOT NULL, seq INTEGER NOT NULL, expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY, principal TEXT NOT NULL,
            url TEXT NOT NULL, name TEXT NOT NULL, args TEXT NOT NULL, secret TEXT NOT NULL,
            expires REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS subscriptions_principal ON subscriptions(principal, expires);
          CREATE TABLE IF NOT EXISTS chat_binding_attempts(
            conversation TEXT NOT NULL, generation INTEGER NOT NULL, principal TEXT NOT NULL,
            failures INTEGER NOT NULL, last_failure REAL, next_allowed REAL, locked_until REAL,
            failure_times TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(conversation,generation,principal));
        """)
        for column, definition in (("delivered", "INTEGER NOT NULL DEFAULT 0"), ("delivered_at", "REAL")):
            try:
                self.execute("ALTER TABLE outputs ADD COLUMN " + column + " " + definition)
            except sqlite3.OperationalError:
                pass  # Column already exists on an upgraded state directory.
        try:
            # Pre-binding subscriptions must be reauthorized by a human-bound
            # subscribe/refresh before they can send metadata or acknowledge it.
            self.execute("ALTER TABLE subscriptions ADD COLUMN bound INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self.execute("ALTER TABLE subscriptions ADD COLUMN binding_generation INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        row = self.execute("SELECT value FROM meta WHERE key='conversation'").fetchone()
        check(row is None or row[0] == conversation, "conversation_state_mismatch")
        self.execute("INSERT OR IGNORE INTO meta VALUES('conversation',?)", (conversation,))
        self.chat_binding = None
        binding_row = self.execute("SELECT value FROM meta WHERE key='chat_binding'").fetchone()
        if binding_row is None:
            now = float(self.clock())
            check(now == now and abs(now) != float("inf"), "chat_binding_clock_invalid", 500)
            code = new_chat_code()
            self.chat_binding = {
                "code": code,
                "code_digest": hashlib.sha256(code.encode("ascii")).hexdigest(),
                "generation": 1,
                "issued_at": now,
                "expires_at": now + BRIDGE_CHAT_LIFETIME,
                # Only a database that already existed with the old stable bearer ID
                # receives this exact, one-time 24-hour compatibility alias.
                "legacy_digest": hashlib.sha256(conversation.encode("ascii")).hexdigest(),
                "legacy_until": now + BRIDGE_CHAT_LIFETIME if row is not None else 0,
                "last_seen": now,
                "last_rotation_request": None,
                "last_rotation_from_generation": None,
            }
            self._save_chat_binding()
            if row is not None:
                self.execute("UPDATE subscriptions SET binding_generation=1 WHERE bound=1 AND binding_generation=0")
        else:
            try:
                self.chat_binding = json.loads(binding_row[0])
            except (TypeError, ValueError):
                raise BridgeError("chat_binding_state_invalid", 500) from None
            binding_fields = {"code", "code_digest", "generation", "issued_at", "expires_at",
                              "legacy_digest", "legacy_until", "last_seen", "last_rotation_request",
                              "last_rotation_from_generation"}
            check(type(self.chat_binding) is dict and set(self.chat_binding) == binding_fields,
                  "chat_binding_state_invalid", 500)
            binding = self.chat_binding
            check(type(binding["code"]) is str
                  and re.fullmatch("[" + BRIDGE_CHAT_ALPHABET + "]{8}", binding["code"]) is not None
                  and type(binding["generation"]) is int and binding["generation"] > 0
                  and all(type(binding[key]) in {int, float} and binding[key] == binding[key]
                          and abs(binding[key]) != float("inf")
                          for key in ("issued_at", "expires_at", "legacy_until", "last_seen"))
                  and binding["expires_at"] == binding["issued_at"] + BRIDGE_CHAT_LIFETIME
                  and binding["issued_at"] <= binding["last_seen"]
                  and 0 <= binding["legacy_until"] <= binding["expires_at"]
                  and type(binding["code_digest"]) is str
                  and re.fullmatch(r"[0-9a-f]{64}", binding["code_digest"]) is not None
                  and hmac.compare_digest(binding["code_digest"], hashlib.sha256(
                      binding["code"].encode("ascii")).hexdigest())
                  and type(binding["legacy_digest"]) is str
                  and re.fullmatch(r"[0-9a-f]{64}", binding["legacy_digest"]) is not None,
                  "chat_binding_state_invalid", 500)
            request_id = binding["last_rotation_request"]
            from_generation = binding["last_rotation_from_generation"]
            check((request_id is None and from_generation is None)
                  or (type(request_id) is str and re.fullmatch(r"[a-zA-Z0-9_-]{16,64}", request_id) is not None
                      and type(from_generation) is int and from_generation + 1 == binding["generation"]),
                  "chat_binding_state_invalid", 500)
        self.execute("DELETE FROM chat_binding_attempts WHERE conversation<>? OR generation<>?",
                     (conversation, self.chat_binding["generation"]))
        self.preference = "SMALL"
        self.pair = None
        self.cleanup()

    def close(self):
        with self.lock:
            self.db.close()

    def execute(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params)

    def executescript(self, sql):
        with self.lock:
            return self.db.executescript(sql)

    def _save_chat_binding(self):
        self.execute("INSERT INTO meta(key,value) VALUES('chat_binding',?)"
                     " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(self.chat_binding, separators=(",", ":")),))

    def _safe_chat_time(self):
        try:
            now = float(self.clock())
        except (TypeError, ValueError, OverflowError):
            raise BridgeError("chat_binding_clock_invalid", 500) from None
        check(now == now and abs(now) != float("inf"), "chat_binding_clock_invalid", 500)
        previous = float(self.chat_binding["last_seen"])
        now = max(now, previous)
        if now > previous:
            self.chat_binding["last_seen"] = now
            self._save_chat_binding()
        return now

    def _attempt(self, principal):
        row = self.execute("SELECT * FROM chat_binding_attempts WHERE conversation=? AND generation=? AND principal=?",
                           (self.conversation, self.chat_binding["generation"], principal)).fetchone()
        if row is None:
            return {"failures": 0, "last_failure": None, "next_allowed": None,
                    "locked_until": None, "failure_times": "[]"}
        value = dict(row)
        failure_limit = BRIDGE_CHAT_BINDING_FAILURES if principal == "*" else BRIDGE_CHAT_PRINCIPAL_FAILURES
        try:
            times = json.loads(value["failure_times"])
        except (TypeError, ValueError):
            raise BridgeError("chat_binding_state_invalid", 500) from None
        check(type(times) is list and len(times) <= BRIDGE_CHAT_BINDING_FAILURES
              and all(type(stamp) in {int, float} and stamp == stamp and abs(stamp) != float("inf")
                      for stamp in times), "chat_binding_state_invalid", 500)
        check(type(value["failures"]) is int and 0 <= value["failures"] <= failure_limit
              and all(value[key] is None or type(value[key]) in {int, float}
                      and value[key] == value[key] and abs(value[key]) != float("inf")
                      for key in ("last_failure", "next_allowed", "locked_until"))
              and type(value["failure_times"]) is str and len(value["failure_times"]) <= 4096,
              "chat_binding_state_invalid", 500)
        failures, last = value["failures"], value["last_failure"]
        if principal == "*":
            valid = (failures == len(times) and times == sorted(times)
                     and last == (times[-1] if times else None)
                     and value["next_allowed"] is None
                     and value["locked_until"] == (last + BRIDGE_CHAT_LOCKOUT
                         if failures == BRIDGE_CHAT_BINDING_FAILURES else None))
        else:
            valid = (not times and (last is not None if failures else last is None)
                     and value["next_allowed"] == (last + BRIDGE_CHAT_BACKOFF[failures - 4]
                         if 4 <= failures < BRIDGE_CHAT_PRINCIPAL_FAILURES else None)
                     and value["locked_until"] == (last + BRIDGE_CHAT_LOCKOUT
                         if failures == BRIDGE_CHAT_PRINCIPAL_FAILURES and last is not None else None))
        check(valid, "chat_binding_state_invalid", 500)
        return value

    def _save_attempt(self, principal, state):
        self.execute("INSERT INTO chat_binding_attempts(conversation,generation,principal,failures,last_failure,"
                     "next_allowed,locked_until,failure_times) VALUES(?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(conversation,generation,principal) DO UPDATE SET failures=excluded.failures,"
                     "last_failure=excluded.last_failure,next_allowed=excluded.next_allowed,"
                     "locked_until=excluded.locked_until,failure_times=excluded.failure_times",
                     (self.conversation, self.chat_binding["generation"], principal, state["failures"],
                      state["last_failure"], state["next_allowed"], state["locked_until"],
                      state["failure_times"]))

    def _binding_is_live(self, generation, now=None):
        now = self._safe_chat_time() if now is None else now
        return generation == self.chat_binding["generation"] and now < self.chat_binding["expires_at"]

    def authorize_chat_binding(self, principal, supplied):
        """Atomically authorize a chat code and persist online-guess controls."""
        check(type(principal) is str and re.fullmatch("[0-9a-f]{32}", principal), "unauthorized", 401)
        with self.lock:
            now = self._safe_chat_time()
            binding = self.chat_binding
            if now >= binding["expires_at"]:
                raise BridgeError("conversation_binding_expired", 403, rpc=-32602)
            aggregate = self._attempt("*")
            try:
                times = json.loads(aggregate["failure_times"])
            except (TypeError, ValueError):
                raise BridgeError("chat_binding_state_invalid", 500) from None
            check(type(times) is list and len(times) <= BRIDGE_CHAT_BINDING_FAILURES
                  and all(type(value) in {int, float} for value in times), "chat_binding_state_invalid", 500)
            times = [float(value) for value in times if value > now - BRIDGE_CHAT_WINDOW]
            principal_state = self._attempt(principal)
            if principal_state["locked_until"] is not None and principal_state["locked_until"] <= now:
                principal_state = {"failures": 0, "last_failure": None, "next_allowed": None,
                                   "locked_until": None, "failure_times": "[]"}
            if aggregate["locked_until"] is not None and aggregate["locked_until"] > now:
                raise BridgeError("conversation_binding_locked", 403, rpc=-32602,
                                  data={"retry_after_seconds": max(1, int(aggregate["locked_until"] - now + .999)),
                                        "scope": "binding"})
            if principal_state["locked_until"] is not None and principal_state["locked_until"] > now:
                raise BridgeError("conversation_binding_locked", 403, rpc=-32602,
                                  data={"retry_after_seconds": max(1, int(principal_state["locked_until"] - now + .999)),
                                        "scope": "principal"})
            if principal_state["next_allowed"] is not None and principal_state["next_allowed"] > now:
                raise BridgeError("conversation_binding_backoff", 403, rpc=-32602,
                                  data={"retry_after_seconds": max(1, int(principal_state["next_allowed"] - now + .999))})

            raw = supplied if type(supplied) is str and len(supplied) <= 64 else ""
            normalized = normalize_chat_code(raw)
            candidate_digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()
            code_matches = hmac.compare_digest(candidate_digest, binding["code_digest"])
            legacy_digest = binding["legacy_digest"]
            alias_matches = (type(legacy_digest) is str and binding.get("legacy_until", 0) > now
                             and hmac.compare_digest(hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest(),
                                                     legacy_digest))
            if code_matches or alias_matches:
                self.execute("DELETE FROM chat_binding_attempts WHERE conversation=? AND generation=? AND principal=?",
                             (self.conversation, binding["generation"], principal))
                return binding["generation"]

            failures = principal_state["failures"] if principal_state["last_failure"] is not None \
                and now - principal_state["last_failure"] < BRIDGE_CHAT_WINDOW else 0
            failures += 1
            principal_state["failures"] = failures
            principal_state["last_failure"] = now
            principal_state["failure_times"] = "[]"
            principal_state["next_allowed"] = None
            principal_state["locked_until"] = None
            if failures >= BRIDGE_CHAT_PRINCIPAL_FAILURES:
                principal_state["locked_until"] = now + BRIDGE_CHAT_LOCKOUT
            elif failures >= 4:
                principal_state["next_allowed"] = now + BRIDGE_CHAT_BACKOFF[min(
                    failures - 4, len(BRIDGE_CHAT_BACKOFF) - 1)]
            times.append(now)
            aggregate["failures"] = len(times)
            aggregate["last_failure"] = now
            aggregate["next_allowed"] = None
            aggregate["locked_until"] = now + BRIDGE_CHAT_LOCKOUT if len(times) >= BRIDGE_CHAT_BINDING_FAILURES else None
            aggregate["failure_times"] = json.dumps(times, separators=(",", ":"))
            # Both counters are one persistent attempt, even if the second write
            # fails. Counted binding errors below are raised only after COMMIT.
            self.execute("BEGIN IMMEDIATE")
            try:
                self._save_attempt(principal, principal_state)
                self._save_attempt("*", aggregate)
                self.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.execute("ROLLBACK")
                raise
            if aggregate["locked_until"] is not None:
                raise BridgeError("conversation_binding_locked", 403, rpc=-32602,
                                  data={"retry_after_seconds": BRIDGE_CHAT_LOCKOUT, "scope": "binding"})
            if principal_state["locked_until"] is not None:
                raise BridgeError("conversation_binding_locked", 403, rpc=-32602,
                                  data={"retry_after_seconds": BRIDGE_CHAT_LOCKOUT, "scope": "principal"})
            if principal_state["next_allowed"] is not None:
                raise BridgeError("conversation_binding_backoff", 403, rpc=-32602,
                                  data={"retry_after_seconds": max(1, int(principal_state["next_allowed"] - now + .999))})
            raise BridgeError("conversation_binding_required", 403, rpc=-32602)

    def chat_binding_status(self, principal):
        with self.lock:
            now = self._safe_chat_time()
            binding = self.chat_binding
            aggregate = self._attempt("*")
            role = self.execute("SELECT role FROM tokens WHERE id=?", (principal,)).fetchone()
            principals = [principal] if role and role["role"] == "AI" else [row[0] for row in self.execute(
                "SELECT id FROM tokens WHERE role='AI' AND state='PAIRED'").fetchall()]
            attempts = [self._attempt(identifier) for identifier in principals]
            locks = [attempt["locked_until"] for attempt in attempts
                     if attempt["locked_until"] is not None and attempt["locked_until"] > now]
            waits = [attempt["next_allowed"] for attempt in attempts
                     if attempt["next_allowed"] is not None and attempt["next_allowed"] > now]
            principal_lock = max(locks) if locks else None
            binding_lock = aggregate["locked_until"] if aggregate["locked_until"] and aggregate["locked_until"] > now else None
            principal_wait = max(waits) if waits else None
            if now >= binding["expires_at"]:
                status, retry_at, scope, code = "EXPIRED", None, None, None
            elif binding_lock or principal_lock:
                status, retry_at, scope, code = "LOCKED", max(value for value in (binding_lock, principal_lock) if value), \
                    "BINDING" if binding_lock and (not principal_lock or binding_lock >= principal_lock) else "PRINCIPAL", binding["code"]
            elif principal_wait:
                status, retry_at, scope, code = "BACKOFF", principal_wait, "PRINCIPAL", binding["code"]
            else:
                status, retry_at, scope, code = "VALID", None, None, binding["code"]
            locked = len(locks)
            return {"version": 1, "instance": self.instance, "conversation": self.conversation,
                    "generation": binding["generation"], "code": code,
                    "issued_at": binding["issued_at"], "expires_at": binding["expires_at"],
                    "server_time": now, "status": status, "retry_at": retry_at,
                    "restriction_scope": scope, "locked_principals": locked}

    def require_chat_binding_generation(self, generation):
        with self.lock:
            check(self._binding_is_live(generation), "conversation_binding_expired", 403)

    def rotate_chat_binding(self, request_id, expected_generation, principal):
        opaque(request_id)
        check(type(expected_generation) is int and expected_generation > 0, "invalid_schema")
        with self.lock:
            now = self._safe_chat_time()
            binding = self.chat_binding
            if binding.get("last_rotation_request") == request_id \
                    and binding.get("last_rotation_from_generation") == expected_generation \
                    and binding["generation"] == expected_generation + 1:
                return self.chat_binding_status(principal)
            check(binding["generation"] == expected_generation, "chat_binding_generation_conflict", 409)
            code = new_chat_code(binding["code"])
            binding.update({"code": code, "code_digest": hashlib.sha256(code.encode("ascii")).hexdigest(),
                            "generation": expected_generation + 1, "issued_at": now,
                            "expires_at": now + BRIDGE_CHAT_LIFETIME, "legacy_until": 0,
                            "last_rotation_request": request_id,
                            "last_rotation_from_generation": expected_generation})
            self._save_chat_binding()
            self.execute("DELETE FROM chat_binding_attempts WHERE conversation=?", (self.conversation,))
            self.execute("DELETE FROM subscriptions WHERE binding_generation<>?", (binding["generation"],))
            return self.chat_binding_status(principal)

    def cleanup(self):
        now = self.clock()
        with self.lock:
            for table in ("proposals", "outputs", "cursors", "subscriptions"):
                self.execute("DELETE FROM " + table + " WHERE expires<=?", (now,))
            self.execute("DELETE FROM tokens WHERE state='PAIR_PENDING' AND deadline<=?", (now,))
            self.execute("DELETE FROM chat_binding_attempts WHERE principal<>'*' AND NOT EXISTS "
                         "(SELECT 1 FROM tokens WHERE tokens.id=chat_binding_attempts.principal "
                         "AND tokens.role='AI' AND tokens.state='PAIRED')")

    def issue(self, role, token=None, pending=False):
        check(role in {"AI", "CONTROLLER"})
        with self.lock:  # Token issuance is serialized against cleanup and limits.
            self.cleanup()
            check(self.execute("SELECT count(*) FROM tokens").fetchone()[0] < 32, "token_limit", 429)
            token = token or secrets.token_hex(32)
            text(token, 64, r"[0-9a-f]{64}")
            identifier = uuid.uuid4().hex
            self.execute("INSERT INTO tokens VALUES(?,?,?,?,?,?)",
                         (hashlib.sha256(token.encode()).hexdigest(), identifier, role,
                          self.conversation, "PAIR_PENDING" if pending else "PAIRED",
                          self.clock() + 120 if pending else None))
        return token, identifier

    def authenticate(self, token, role, pending=False):
        # Token shape is checked before hashing and never reflected to the caller.
        check(type(token) is str and re.fullmatch(r"[0-9a-f]{64}", token), "unauthorized", 401)
        with self.lock:  # Consume the shared-connection cursor inside the same lock.
            row = self.execute("SELECT * FROM tokens WHERE hash=?",
                               (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            check(row is not None and row["role"] == role and row["conversation"] == self.conversation
                  and (row["state"] == "PAIRED" or pending and row["state"] == "PAIR_PENDING"
                       and row["deadline"] > self.clock()), "unauthorized", 401)
            return row

    def revoke(self, identifier):
        opaque(identifier)
        with self.lock:
            self.execute("DELETE FROM tokens WHERE id=?", (identifier,))
            self.execute("DELETE FROM chat_binding_attempts WHERE conversation=? AND principal=?",
                         (self.conversation, identifier))

    def open_pair(self):
        code = "".join(secrets.choice(BRIDGE_BASE32) for _ in range(12))
        self.pair = {"hash": hashlib.sha256(code.encode()).digest(),
                     "deadline": self.clock() + 120, "failures": 0}
        return "-".join(code[i:i + 4] for i in range(0, 12, 4))

    def claim(self, value):
        exact(value, {"code", "request_id"})
        opaque(value["request_id"])
        code = text(value["code"], 16).replace("-", "").upper()
        with self.lock:  # One pairing window is consumed exactly once.
            check(self.pair is not None and self.pair["deadline"] > self.clock()
                  and self.pair["failures"] < 5, "pair_closed", 403)
            if not hmac.compare_digest(hashlib.sha256(code.encode()).digest(), self.pair["hash"]):
                self.pair["failures"] += 1
                raise BridgeError("pair_code_invalid", 403)
            self.pair = None
            token, identifier = self.issue("CONTROLLER", pending=True)
        return {"version": 1, "token": token, "controller_id": identifier,
                "conversation": self.conversation, "instance": self.instance, "state": "PAIR_PENDING"}

    def confirm(self, token):
        with self.lock:
            row = self.authenticate(token, "CONTROLLER", pending=True)
            # One active controller per bridge conversation in this single-user MVP.
            self.execute("DELETE FROM tokens WHERE role='CONTROLLER' AND id<>?", (row["id"],))
            self.execute("UPDATE tokens SET state='PAIRED',deadline=NULL WHERE id=?", (row["id"],))
        return self.status(token)

    def status(self, token):
        row = self.authenticate(token, "CONTROLLER")
        return {"version": 1, "instance": self.instance, "conversation": row["conversation"],
                "controller_id": row["id"], "state": "PAIRED", "retention_seconds": BRIDGE_RETENTION}

    def cursor_position(self, value, owner, kind):
        if not value:
            return 0
        opaque(value)
        row = self.execute("SELECT * FROM cursors WHERE id=?", (value,)).fetchone()
        check(row is not None and row["owner"] == owner and row["kind"] == kind
              and row["expires"] > self.clock(), "invalid_cursor")
        return row["seq"]

    def cursor(self, owner, kind, seq):
        with self.lock:
            # Reuse cursors at the same position; empty polls do not consume memory.
            row = self.execute("SELECT id FROM cursors WHERE owner=? AND kind=? AND seq=? AND expires>?",
                               (owner, kind, seq, self.clock())).fetchone()
            if row:
                return row[0]
            self.cleanup()
            check(self.execute("SELECT count(*) FROM cursors").fetchone()[0] < 2048, "cursor_limit", 429)
            value = secrets.token_hex(16)
            self.execute("INSERT INTO cursors VALUES(?,?,?,?,?)", (value, owner, kind, seq,
                                                                   self.clock() + BRIDGE_RETENTION))
            return value

    def submit(self, value):
        exact(value, {"request_id", "text"})
        request_id = opaque(value["request_id"])
        command = text(value["text"], 4096)
        normalized = command.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        check(normalized.strip() and len((normalized + "\n").encode()) <= 4096
              and len(normalized.split("\n")) <= 50, "command_limit")
        check(all(c in {" ", "\n"} or not unicodedata.category(c).startswith(("C", "Z"))
                  for c in normalized), "command_character")
        with self.lock:  # Idempotent submit stays atomic under threaded requests.
            self.cleanup()
            previous = self.execute("SELECT * FROM proposals WHERE request=?", (request_id,)).fetchone()
            if previous:
                check(previous["text"] == command, "idempotency_conflict", 409)
                return {"submission_id": previous["id"], "state": "ACCEPTED", "expires_at": previous["expires"]}
            # Queued = not yet observed over any controller cursor. No review state is published.
            observed = self.execute("SELECT coalesce(max(seq),0) FROM cursors WHERE kind='INBOX'").fetchone()[0]
            check(self.execute("SELECT count(*) FROM proposals WHERE seq>?", (observed,)).fetchone()[0] < 20,
                  "inbox_full", 429)
            check(self.execute("SELECT count(*) FROM proposals").fetchone()[0] < 1024, "message_limit", 429)
            identifier, deadline = uuid.uuid4().hex, self.clock() + BRIDGE_RETENTION
            try:
                self.execute("INSERT INTO proposals(id,request,text,expires) VALUES(?,?,?,?)",
                             (identifier, request_id, command, deadline))
            except sqlite3.IntegrityError:
                previous = self.execute("SELECT * FROM proposals WHERE request=?", (request_id,)).fetchone()
                check(previous is not None and previous["text"] == command, "idempotency_conflict", 409)
                return {"submission_id": previous["id"], "state": "ACCEPTED", "expires_at": previous["expires"]}
        return {"submission_id": identifier, "state": "ACCEPTED", "expires_at": deadline}

    def inbox(self, token, cursor):
        row = self.authenticate(token, "CONTROLLER")
        position = self.cursor_position(cursor, row["id"], "INBOX")
        proposal = self.execute("SELECT * FROM proposals WHERE seq>? AND expires>? ORDER BY seq LIMIT 1",
                                (position, self.clock())).fetchone()
        if not proposal:
            return {"version": 1, "instance": self.instance, "conversation": self.conversation,
                    "cursor": cursor or self.cursor(row["id"], "INBOX", position), "messages": []}
        return {"version": 1, "instance": self.instance, "conversation": self.conversation,
                "cursor": self.cursor(row["id"], "INBOX", proposal["seq"]),
                "messages": [{"id": proposal["id"], "text": proposal["text"], "expires_at": proposal["expires"]}]}

    def approved_output(self, value):
        exact(value, {"version", "conversation", "idempotency_key", "text", "submission_id", "approved_at"})
        check(type(value["version"]) is int and value["version"] == 1)
        check(value["conversation"] == self.conversation, "wrong_conversation", 403)
        key = opaque(value["idempotency_key"])
        check(type(value["approved_at"]) in {int, float}
              and self.clock() - BRIDGE_RETENTION < value["approved_at"] <= self.clock() + 300,
              "output_expired", 410)
        content = text(value["text"], BRIDGE_OUTPUT_LIMIT)
        check(content and all(c in {" ", "\n"} or not unicodedata.category(c).startswith(("C", "Z"))
                              for c in content), "output_character")
        check(len(encoded({"text": content})) <= BRIDGE_BODY_LIMIT - 2048, "output_envelope_limit", 413)
        if value["submission_id"] is not None:
            opaque(value["submission_id"])
            check(self.execute("SELECT id FROM proposals WHERE id=? AND expires>?",
                               (value["submission_id"], self.clock())).fetchone(), "submission_expired", 410)
        # Exclude no fields: the same key is bound to the entire exact approved body.
        digest = hashlib.sha256(encoded(value)).hexdigest()
        with self.lock:
            previous = self.execute("SELECT * FROM outputs WHERE idem=?", (key,)).fetchone()
            if previous:
                check(previous["expires"] > self.clock(), "output_expired", 410)
                check(hmac.compare_digest(previous["hash"], digest), "idempotency_conflict", 409)
                return {"version": 1, "output_id": previous["id"], "state": "ACKNOWLEDGED"}
            self.cleanup()
            check(self.execute("SELECT count(*) FROM outputs").fetchone()[0] < 256, "output_limit", 429)
            identifier = uuid.uuid4().hex
            deadline = min(self.clock() + BRIDGE_RETENTION, value["approved_at"] + BRIDGE_RETENTION)
            self.execute("INSERT INTO outputs(id,idem,hash,submission,text,expires)" " "
                         "VALUES(?,?,?,?,?,?)",
                         (identifier, key, digest, value["submission_id"], content, deadline))
            callback, payload = self.on_new_output, (identifier, value["submission_id"], deadline)
        if callback is not None:
            try:
                callback(*payload)
            except Exception:
                pass  # An approved output never depends on its event emission path.
        return {"version": 1, "output_id": identifier, "state": "ACKNOWLEDGED"}

    def submission_status(self, submission_id):
        now = self.clock()
        with self.lock:
            row = self.execute("SELECT expires FROM proposals WHERE id=? AND expires>?",
                               (submission_id, now)).fetchone()
            check(row is not None, "submission_unavailable", 404)
            shared = self.execute("SELECT id FROM outputs WHERE submission=? AND expires>? AND delivered=0 LIMIT 1",
                                  (submission_id, now)).fetchone()
            return {"state": "ACCEPTED", "approved_output_available": bool(shared)}

    def ai_output(self, principal, value):
        exact(value, set(), {"submission_id", "cursor"})
        cursor = value.get("cursor", "")
        submission = value.get("submission_id")
        if submission is not None:
            opaque(submission)
        with self.lock:
            position = self.cursor_position(cursor, principal["id"], "OUTPUT")
            if submission is None:
                # Un-bound outputs stay readable on the canonical pull even after a
                # confirmed push: there is no submission readback to recover them from.
                row = self.execute("SELECT * FROM outputs WHERE seq>? AND expires>?"
                                   " AND (delivered=0 OR submission IS NULL)"
                                   " ORDER BY seq LIMIT 1", (position, self.clock())).fetchone()
                if row is None:
                    gap = self.execute("SELECT max(seq) AS top FROM outputs WHERE seq>? AND expires>?"
                                       " AND delivered=1 AND submission IS NOT NULL",
                                       (position, self.clock())).fetchone()
                    if gap and gap["top"] is not None and gap["top"] > position:
                        position = gap["top"]
            else:
                # An explicitly named output stays within the retention as an authorized readback.
                row = self.execute("SELECT * FROM outputs WHERE seq>? AND expires>? AND submission=?"
                                   " ORDER BY seq LIMIT 1", (position, self.clock(), submission)).fetchone()
            result = [] if row is None else [{"output_id": row["id"], "submission_id": row["submission"],
                                              "text": row["text"], "expires_at": row["expires"]}]
            return {"outputs": result, "cursor": self.cursor(principal["id"], "OUTPUT", row["seq"] if row else position)}

    @staticmethod
    def subscription_identity(principal_id, url, name, args_json):
        digest = hashlib.sha256("|".join((principal_id, url, name, args_json)).encode("utf-8")).hexdigest()
        return "sub_" + digest

    def count_subscriptions(self, principal_id):
        with self.lock:
            return self.execute("SELECT count(*) AS total FROM subscriptions WHERE principal=? AND expires>?",
                                (principal_id, self.clock())).fetchone()["total"]

    def store_subscription(self, principal_id, url, name, args_json, secret, expires, *, bound=False,
                           binding_generation=None):
        identifier = self.subscription_identity(principal_id, url, name, args_json)
        with self.lock:
            if binding_generation is None:
                binding_generation = self.chat_binding["generation"]
            self.require_chat_binding_generation(binding_generation)
            check(self.execute("SELECT id FROM tokens WHERE id=? AND role='AI'"
                               " AND conversation=? AND state='PAIRED'",
                               (principal_id, self.conversation)).fetchone() is not None,
                  "unauthorized", 401)
            existing = self.find_subscription(principal_id, url, name, args_json)
            if (existing is None or existing["expires"] <= self.clock()) \
                    and self.count_subscriptions(principal_id) >= BRIDGE_EVENT_SUBSCRIPTION_LIMIT:
                raise BridgeError("subscription_limit", rpc=-32013,
                                  data={"limit": "subscriptions", "max": BRIDGE_EVENT_SUBSCRIPTION_LIMIT})
            self.execute("INSERT INTO subscriptions(id,principal,url,name,args,secret,expires,bound,binding_generation)"
                         " VALUES(?,?,?,?,?,?,?,?,?)"
                         " ON CONFLICT(id) DO UPDATE SET url=excluded.url, secret=excluded.secret,"
                         " expires=excluded.expires, bound=excluded.bound,"
                         " binding_generation=excluded.binding_generation",
                         (identifier, principal_id, url, name, args_json, secret, expires, int(bound), binding_generation))
        return identifier

    def find_subscription(self, principal_id, url, name, args_json):
        with self.lock:
            row = self.execute("SELECT * FROM subscriptions WHERE id=?",
                               (self.subscription_identity(principal_id, url, name, args_json),)).fetchone()
            return dict(row) if row else None

    def delete_subscription(self, principal_id, identifier):
        return self.execute("DELETE FROM subscriptions WHERE id=? AND principal=?",
                            (identifier, principal_id)).rowcount

    def matching_subscriptions(self, submission_id, now):
        with self.lock:
            now = max(float(now), self._safe_chat_time())
            if now >= self.chat_binding["expires_at"]:
                return []
            rows = self.execute("SELECT * FROM subscriptions WHERE expires>? AND bound=1 AND binding_generation=?"
                                " ORDER BY id", (now, self.chat_binding["generation"])).fetchall()
        result = []
        for row in rows:
            args = json.loads(row["args"]) if row["args"] else {}
            if submission_id is None and args.get("submission_id") is not None:
                continue
            if submission_id is not None and args.get("submission_id") not in (None, submission_id):
                continue
            result.append(dict(row))
        return result

    def active_subscription(self, identifier, now):
        """Subscription row only while its AI principal is still paired and authorized."""
        with self.lock:
            now = max(float(now), self._safe_chat_time())
            if now >= self.chat_binding["expires_at"]:
                return None
            row = self.execute("SELECT s.* FROM subscriptions s JOIN tokens t ON t.id = s.principal"
                               " WHERE s.id=? AND s.expires>? AND s.bound=1 AND s.binding_generation=?"
                               " AND t.role='AI' AND t.conversation=? AND t.state='PAIRED'",
                               (identifier, now, self.chat_binding["generation"], self.conversation)).fetchone()
            return dict(row) if row else None

    def mark_delivered(self, output_id, subscription_id, expected_generation):
        opaque(output_id)
        with self.lock:
            if not self._binding_is_live(expected_generation):
                return False
            if self.active_subscription(subscription_id, self.clock()) is None:
                return False
            row = self.execute("SELECT delivered FROM outputs WHERE id=?", (output_id,)).fetchone()
            if row is None:
                return False
            if row["delivered"]:
                return True
            # The SQL authorization predicate also protects against revocation by
            # another SQLite connection (e.g. the local revoke CLI) at ACK time.
            now = self._safe_chat_time()
            if not self._binding_is_live(expected_generation, now):
                return False
            return bool(self.execute("UPDATE outputs SET delivered=1, delivered_at=? WHERE id=?"
                                     " AND EXISTS (SELECT 1 FROM subscriptions s JOIN tokens t ON t.id=s.principal"
                                     " WHERE s.id=? AND s.expires>? AND s.bound=1 AND s.binding_generation=?"
                                     " AND t.role='AI' AND t.conversation=? AND t.state='PAIRED')"
                                     " AND EXISTS (SELECT 1 FROM meta WHERE key='chat_binding'"
                                     " AND json_extract(value,'$.generation')=?"
                                     " AND json_extract(value,'$.expires_at')>?)",
                                     (now, output_id, subscription_id, now, expected_generation,
                                      self.conversation, expected_generation, now)).rowcount)


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


BRIDGE_ID_SCHEMA = {"type": "string", "minLength": 16, "maxLength": 64, "pattern": "^[a-zA-Z0-9_-]+$"}
BRIDGE_TOOLS = [
    {"name": "submit_proposal", "description": "Queue a terminal command for local human review. ACCEPTED means received, never executed. No terminal access.",
     "inputSchema": object_schema({"request_id": BRIDGE_ID_SCHEMA, "text": {"type": "string", "maxLength": 4096}}, ("request_id", "text")),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_submission_status", "description": "Return transport acceptance and whether a not-yet-delivered human-approved output exists. No execution, prompt or review status.",
     "inputSchema": object_schema({"submission_id": BRIDGE_ID_SCHEMA}, ("submission_id",)),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_approved_output", "description": "Read only output explicitly shared by the human with this conversation. Empty means none available; do not infer execution.",
     "inputSchema": object_schema({"submission_id": BRIDGE_ID_SCHEMA, "cursor": BRIDGE_ID_SCHEMA}),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "get_preferences", "description": "Read the human's preferred command block size: SINGLE, SMALL, MEDIUM or LARGE. A comfort preference, never permission.",
     "inputSchema": object_schema({}),
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]
for bridge_tool in BRIDGE_TOOLS:
    # Tunnel authentication identifies an app, not a particular ChatGPT chat.
    # This locally selected capability is disclosed by the human to one chat.
    # It is never discoverable from any AI-facing tool or status endpoint.
    bridge_tool["inputSchema"]["properties"]["conversation"] = dict(BRIDGE_CHAT_CODE_SCHEMA)
    bridge_tool["inputSchema"]["required"].append("conversation")


BRIDGE_EVENT_NAME = "approved_output.available"
BRIDGE_EVENT_DEFINITION = {
    "name": BRIDGE_EVENT_NAME,
    "description": "Signals that human-approved terminal output is available for this conversation. "
                   "The payload carries correlation metadata only; the approved text remains on the "
                   "canonical approved-output path.",
    "delivery": ["webhook"],
    "inputSchema": object_schema({"submission_id": BRIDGE_ID_SCHEMA,
                                  "conversation": dict(BRIDGE_CHAT_CODE_SCHEMA)},
                                 ("conversation",)),
    "payloadSchema": {"type": "object",
                      "properties": {"submission_id": {"type": ["string", "null"]},
                                     "output_id": {"type": "string"},
                                     "expires_at": {"type": "number"}},
                      "required": ["submission_id", "output_id", "expires_at"],
                      "additionalProperties": False},
}
BRIDGE_EVENT_TTL = 24 * 3600 * 1000
BRIDGE_EVENT_TTL_MIN = 60 * 1000
BRIDGE_EVENT_TTL_MAX = 7 * 24 * 3600 * 1000
BRIDGE_EVENT_SUBSCRIPTION_LIMIT = 64
BRIDGE_WEBHOOK_VERIFICATION_TTL = 600


def iso_utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def webhook_address_allowed(address):
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return False
    return value.is_global and not value.is_loopback and not value.is_link_local


def webhook_addresses(hostname, port):
    return sorted({item[4][0] for item
                   in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)})


def standard_webhook_headers(identifier, webhook_id, secret, body):
    """Standard Webhooks signature headers over the exact request body bytes."""
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (ValueError, binascii.Error):
        raise BridgeError("invalid_webhook_secret") from None
    timestamp = str(int(time.time()))
    signed = (webhook_id + "." + timestamp + ".").encode("ascii") + body
    signature = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    return {"Content-Type": "application/json", "webhook-id": webhook_id, "webhook-timestamp": timestamp,
            "webhook-signature": signature}

class BridgeResponse:
    __slots__ = ("status", "value")

    def __init__(self, status=200, value=None):
        self.status, self.value = status, value


class WebhookFailure(Exception):
    """Transport failure categories only; never payloads, URLs or credentials."""
    __slots__ = ("reason", "retryable")

    def __init__(self, reason, retryable=True):
        super().__init__(reason)
        self.reason, self.retryable = reason, retryable


def webhook_tls_context():
    """Production: full SSL verification against the public callback host."""
    return ssl.create_default_context()


class WebhookConnection(http.client.HTTPSConnection):
    """HTTPS to a callback: no proxies, no redirects, validated address binding."""
    def __init__(self, address, hostname, port, timeout=10):
        super().__init__(address, port, timeout=timeout, context=webhook_tls_context())
        self.tls_hostname = hostname

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.tls_hostname)


class QueryDict:
    __slots__ = ("pairs",)

    def __init__(self, pairs):
        self.pairs = tuple(pairs)

    def keys(self):
        seen, keys = set(), []
        for key, _ in self.pairs:
            if key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def __iter__(self):
        return iter(self.keys())

    def __bool__(self):
        return bool(self.pairs)

    def __contains__(self, key):
        return any(k == key for k, _ in self.pairs)

    def get(self, key, default=None):
        for candidate, value in self.pairs:
            if candidate == key:
                return value
        return default

    def getall(self, key):
        return [value for candidate, value in self.pairs if candidate == key]


class BridgeRequest:
    """Stdlib view of one HTTP request, mirroring the previous handler surface."""
    __slots__ = ("handler", "method", "path", "path_qs", "host", "query", "headers", "kind")

    def __init__(self, handler, kind):
        parsed = urlsplit(handler.path)
        self.handler, self.kind = handler, kind
        self.method, self.path, self.path_qs = handler.command, parsed.path, handler.path
        self.headers = handler.headers
        self.host = handler.headers.get("Host", "")
        self.query = QueryDict(parse_qsl(parsed.query, keep_blank_values=True))

    @property
    def content_type(self):
        return self.headers.get("Content-Type", "").split(";")[0].strip().lower()


class BridgeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    tls_context = None

    def __init__(self, address, handler, tls=None):
        super().__init__(address, handler)
        self.tls_context = tls
        self.connection_slots = handler.service.connection_slots

    def get_request(self):
        # No TLS handshake here: a slow or malicious client must not stall the
        # accept loop; the handshake happens in the per-request thread.
        accepted = self.socket.accept()
        accepted[0].settimeout(5)
        return accepted

    def process_request(self, request, client_address):
        # Admission precedes ThreadingMixIn's thread creation, TLS and parsing.
        # Both listeners share the service's connection budget.
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class BridgeRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AdminInTheLoopBridge"
    protocol_version = "HTTP/1.1"
    service = None
    kind = None

    def log_message(self, *args):
        pass

    def log_error(self, *args):
        pass

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def send_error(self, code, message=None, explain=None):
        # Parse-level failures (bad request line, unsupported verb, oversized header)
        # get the same fixed JSON body instead of an HTML error page.
        body = encoded({"error": "http_rejected"})
        self.close_connection = True
        try:
            self.send_response_only(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def setup(self):
        # Runs on the request thread; the handshake can stall only this thread.
        deadline = time.monotonic() + 5
        self.request.settimeout(5)
        if self.server.tls_context is not None and not isinstance(self.request, ssl.SSLSocket):
            try:
                self.request = self.server.tls_context.wrap_socket(self.request, server_side=True)
            except (OSError, ssl.SSLError):
                try:
                    self.request.close()
                except OSError:
                    pass
                # Handshake failure: present an empty clean stream so the handler
                # terminates without touching the listener.
                dead = io.BytesIO(b"")
                self.request = None
                self.rfile = dead
                self.wfile = io.BytesIO()
                self.close_connection = True
                return
        super().setup()
        # Bounded total request size: request line, headers and body together.
        self.rfile = BoundedReader(self.rfile, BRIDGE_BODY_LIMIT + 4096, self.connection, deadline)

    def read_body(self, count):
        raw = bytearray()
        try:
            while count > 0:
                chunk = self.rfile.read1(min(16384, count))
                if not chunk:
                    raise BridgeError("body_timeout", 408)
                raw.extend(chunk)
                count -= len(chunk)
        except TimeoutError as failure:
            raise BridgeError("body_timeout", 408) from None
        return bytes(raw)

    def _dispatch(self):
        service, kind = self.service, self.kind
        try:
            self.connection.settimeout(5)
            request = BridgeRequest(self, kind)
            try:
                check(not request.headers.get_all("Origin"), "browser_origin_forbidden", 403)
                check(request.host in service.hosts(kind), "invalid_host", 403)
                check(len(request.path_qs) <= 2048, "url_limit", 414)
                for key in ("Authorization", "X-AITL-AI-Key", "Content-Type", "MCP-Protocol-Version", "Host",
                            "Content-Length", "Transfer-Encoding"):
                    check(len(request.headers.get_all(key, [])) <= 1, "duplicate_header")
                service.throttle("GLOBAL", 600)
                service.enter()
                try:
                    response = service.route(request)
                finally:
                    service.leave()
            except BridgeError as failure:
                response = service.json({"error": failure.code}, failure.status)
            self.connection.settimeout(35)
            self._respond(response.status, response.value)
        except BridgeError as failure:
            self._respond(failure.status, {"error": failure.code})
        except Exception:
            self._respond(500, {"error": "service_failure"})

    def _respond(self, status, value=None):
        body = b"" if value is None else encoded(value)
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except OSError:
            pass


class BoundedReader:
    """Byte-capped request reader with a monotonic TLS/header/body total deadline."""
    def __init__(self, inner, limit, connection, deadline):
        self.inner, self.limit, self.used = inner, limit, 0
        self.connection, self.deadline = connection, deadline
        self.pending = bytearray()

    def readable(self):
        return True

    def close(self):
        self.inner.close()

    @property
    def closed(self):
        return self.inner.closed

    def _guard(self, want):
        if self.used + want > self.limit:
            raise BridgeError("body_limit", 413)

    def read(self, size=-1):
        want = self.limit + 1 if size is None or size < 0 else size
        self._guard(want)
        result = bytearray()
        while len(result) < want:
            chunk = self.read1(want - len(result))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def _receive(self, size):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.connection.settimeout(remaining)
        # BufferedReader.read/readline may perform unlimited underlying receives
        # under an inactivity timeout. read1 returns after at most one receive.
        chunk = self.inner.read1(size)
        if time.monotonic() >= self.deadline:
            raise TimeoutError
        return chunk

    def readline(self, size=-1):
        want = self.limit + 1 if size is None or size < 0 else size
        self._guard(want)
        result = bytearray()
        while len(result) < want:
            if time.monotonic() >= self.deadline:
                raise TimeoutError
            if not self.pending:
                self.pending.extend(self._receive(min(16384, self.limit - self.used)))
                if not self.pending:
                    break
            newline = self.pending.find(b"\n")
            count = min(want - len(result), newline + 1 if newline >= 0 else len(self.pending))
            result.extend(self.pending[:count])
            del self.pending[:count]
            self.used += count
            if result.endswith(b"\n"):
                break
        return bytes(result)

    def read1(self, size=-1):
        want = self.limit + 1 if size is None or size < 0 else size
        self._guard(want)
        if time.monotonic() >= self.deadline:
            raise TimeoutError
        if self.pending:
            chunk = bytes(self.pending[:want])
            del self.pending[:want]
        else:
            chunk = self._receive(want)
        self.used += len(chunk)
        return chunk


class BridgeService:
    """Single-process bridge service on the standard library only."""
    WEBHOOK_ATTEMPT_LIMIT = 3
    WEBHOOK_RETRY_DELAYS = (2.0, 10.0)
    WEBHOOK_JOB_LIMIT = 64

    def __init__(self, store):
        self.store = store
        store.on_new_output = self.queue_output_events
        self.changed = threading.Event()
        self.stopped = True
        self.inflight = 0
        self.inflight_lock = threading.Lock()
        self.connection_slots = threading.BoundedSemaphore(64)
        self.rate, self.rate_lock = {}, threading.Lock()
        self.controller_hosts, self.mcp_hosts = set(), set()
        self.jobs, self.jobs_cv = [], threading.Condition()
        self.verified = {}
        self.webhook_thread = threading.Thread(target=self.webhook_loop, name="aitl-webhook", daemon=True)
        self.routes = {
            "CONTROLLER": {"/v1/status": (self._controller_status, "GET"),
                            "/v1/inbox": (self._controller_inbox, "GET"),
                            "/v1/approved-output": (self._controller_output, "POST"),
                            "/v1/preferences": (self._controller_preferences, "PUT"),
                            "/v1/chat-binding": (self._controller_chat_binding, "GET"),
                            "/v1/chat-binding/rotate": (self._controller_chat_binding_rotate, "POST"),
                            "/v1/pair/claim": (self._controller_claim, "POST"),
                            "/v1/pair/confirm": (self._controller_confirm, "POST")},
            "MCP": {"/mcp": (self.mcp, "POST")},
        }

    def start(self):
        self.stopped = False
        self.webhook_thread.start()

    def stop(self):
        self.stopped = True
        with self.jobs_cv:
            self.jobs_cv.notify_all()

    def wait_idle(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.inflight_lock:
                if self.inflight == 0:
                    return True
            time.sleep(0.02)
        return False

    def hosts(self, kind):
        return self.mcp_hosts if kind == "MCP" else self.controller_hosts

    def throttle(self, key, limit=180):
        now = time.monotonic()
        with self.rate_lock:
            minute, count = self.rate.get(key, (now, 0))
            if now - minute >= 60:
                minute, count = now, 0
            check(count < limit, "rate_limit", 429)
            self.rate[key] = (minute, count + 1)

    def enter(self):
        if self.stopped:
            raise BridgeError("busy", 503)
        with self.inflight_lock:
            check(self.inflight < 64, "busy", 503)
            self.inflight += 1

    def leave(self):
        with self.inflight_lock:
            self.inflight -= 1

    def route(self, request):
        entry = self.routes[request.kind].get(request.path)
        if entry is None:
            raise BridgeError("http_rejected", 404)
        handler, method = entry
        if request.method != method:
            raise BridgeError("http_rejected", 405)
        return handler(request)

    def json(self, value, status=200):
        return BridgeResponse(status, value)

    def body(self, request):
        check(request.content_type == "application/json", "content_type", 415)
        check(not request.headers.get("Content-Encoding"), "content_encoding", 415)
        # Fixed framing: only an exact, single Content-Length is accepted; the
        # Transfer-Encoding family is rejected outright (smuggling surface).
        check(not request.headers.get("Transfer-Encoding"), "transfer_encoding_rejected", 411)
        length = request.headers.get("Content-Length", "")
        check(length.isdigit(), "body_length_required", 411)
        value = int(length)
        check(value <= BRIDGE_BODY_LIMIT, "body_limit", 413)
        return parse(request.handler.read_body(value))

    def principal(self, request, role, pending=False):
        if role == "AI":
            token = request.headers.get("X-AITL-AI-Key", "")
        else:
            auth = request.headers.get("Authorization", "")
            check(auth.startswith("Bearer "), "unauthorized", 401)
            token = auth[7:]
        row = self.store.authenticate(token, role, pending)
        self.throttle(row["id"])
        return token, row

    def _controller_status(self, request):
        token, _ = self.principal(request, "CONTROLLER")
        check(not request.query)
        return self.json(self.store.status(token))

    def _controller_chat_binding(self, request):
        _, principal = self.principal(request, "CONTROLLER")
        check(not request.query)
        return self.json(self.store.chat_binding_status(principal["id"]))

    def _controller_chat_binding_rotate(self, request):
        _, principal = self.principal(request, "CONTROLLER")
        value = self.body(request)
        _, principal = self.principal(request, "CONTROLLER")
        exact(value, {"version", "request_id", "expected_generation"})
        check(type(value["version"]) is int and value["version"] == 1)
        return self.json(self.store.rotate_chat_binding(value["request_id"],
                                                        value["expected_generation"], principal["id"]))

    def _controller_inbox(self, request):
        token, _ = self.principal(request, "CONTROLLER")
        check(set(request.query) <= {"cursor", "wait"}
              and all(len(request.query.getall(key)) == 1 for key in request.query))
        cursor = request.query.get("cursor", "")
        wait = request.query.get("wait", "25")
        check(re.fullmatch(r"[0-9]{1,2}", wait) and int(wait) <= 25, "wait_limit")
        self.changed.clear()
        value = self.store.inbox(token, cursor)
        if not value["messages"] and int(wait):
            deadline = time.monotonic() + int(wait)
            while time.monotonic() < deadline and not self.stopped:
                if self.changed.wait(0.2):
                    break
            value = self.store.inbox(token, cursor)  # Recheck revoked auth and expiry after waiting.
        return self.json(value)

    def _controller_output(self, request):
        self.principal(request, "CONTROLLER")
        value = self.body(request)
        self.principal(request, "CONTROLLER")
        return self.json(self.store.approved_output(value))

    def _controller_preferences(self, request):
        self.principal(request, "CONTROLLER")
        value = self.body(request)
        self.principal(request, "CONTROLLER")
        exact(value, {"version", "block_size"})
        check(type(value["version"]) is int and value["version"] == 1
              and value["block_size"] in BRIDGE_BLOCK_SIZES)
        self.store.preference = value["block_size"]
        return self.json({"version": 1, "block_size": self.store.preference})

    def _controller_claim(self, request):
        self.throttle("PAIR", 10)
        return self.json(self.store.claim(self.body(request)))

    def _controller_confirm(self, request):
        token, _ = self.principal(request, "CONTROLLER", pending=True)
        value = self.body(request)
        exact(value, {"version"})
        check(type(value["version"]) is int and value["version"] == 1)
        return self.json(self.store.confirm(token))

    def mcp(self, request):
        _, principal = self.principal(request, "AI")
        value = self.body(request)
        _, principal = self.principal(request, "AI")
        exact(value, {"jsonrpc", "method"}, {"id", "params"})
        check(value["jsonrpc"] == "2.0")
        method = text(value["method"], 80)
        rid = value.get("id")
        check("id" not in value or type(rid) is int and -(2**53) < rid < 2**53
              or type(rid) is str and len(rid) <= 64, "invalid_rpc_id")
        params = value.get("params", {})
        check(type(params) is dict)
        header = request.headers.get("MCP-Protocol-Version", "2025-03-26")
        check(header in BRIDGE_VERSIONS, "unsupported_protocol")
        modern = header == "2026-07-28"
        if modern:
            meta = params.get("_meta", {})
            check(type(meta) is dict and meta.get("io.modelcontextprotocol/protocolVersion") == header
                  and type(meta.get("io.modelcontextprotocol/clientCapabilities")) is dict, "invalid_rpc_meta")
        # Metadata is passive, never used as a capability or human approval.
        params = {key: val for key, val in params.items() if key != "_meta"}
        if method.startswith("notifications/"):
            check("id" not in value and method in {"notifications/initialized", "notifications/cancelled"})
            return self.json(None, 202)
        check("id" in value, "rpc_id_required")
        try:
            if method == "initialize" and not modern:
                exact(params, {"protocolVersion", "capabilities", "clientInfo"})
                check(type(params["capabilities"]) is dict and type(params["clientInfo"]) is dict)
                proposed = text(params["protocolVersion"], 16)
                result = {"protocolVersion": proposed if proposed in BRIDGE_VERSIONS[1:] else "2025-11-25",
                          "capabilities": {"tools": {"listChanged": False}}, "serverInfo": BRIDGE_SERVER_INFO}
            elif method == "server/discover" and modern:
                exact(params, set())
                result = {"supportedVersions": list(BRIDGE_VERSIONS),
                          "capabilities": {"tools": {"listChanged": False}, "events": {}},
                          "resultType": "complete", "cacheScope": "private", "ttlMs": 0}
            elif method == "ping":
                exact(params, set())
                result = {}
            elif method == "tools/list":
                exact(params, set())
                result = {"tools": BRIDGE_TOOLS}
            elif method == "tools/call":
                exact(params, {"name"}, {"arguments"})
                name = text(params["name"], 80)
                args = params.get("arguments", {})
                with self.store.lock:
                    if type(args) is not dict:
                        self.store.authorize_chat_binding(principal["id"], None)
                    self.store.authorize_chat_binding(principal["id"], args.get("conversation"))
                    args = {key: val for key, val in args.items() if key != "conversation"}
                    if name == "submit_proposal":
                        data = self.store.submit(args)
                        self.changed.set()
                    elif name == "get_submission_status":
                        exact(args, {"submission_id"})
                        opaque(args["submission_id"])
                        data = self.store.submission_status(args["submission_id"])
                    elif name == "get_approved_output":
                        data = self.store.ai_output(principal, args)
                    elif name == "get_preferences":
                        exact(args, set())
                        data = {"block_size": self.store.preference}
                    else:
                        raise BridgeError("unknown_tool")
                # Every MCP client can read content; some hosts discard structuredContent.
                # Put approved text into its own plaintext block once, not into a nested
                # JSON string or twice into the response. Metadata names its block index.
                if name == "get_approved_output":
                    metadata = {"outputs": [], "cursor": data["cursor"]}
                    blocks = [{"type": "text", "text": ""}]
                    for item in data["outputs"]:
                        metadata["outputs"].append({key: val for key, val in item.items() if key != "text"}
                                                    | {"text_content_index": len(blocks)})
                        blocks.append({"type": "text", "text": item["text"]})
                    blocks[0]["text"] = encoded(metadata).decode()
                    result = {"content": blocks, "structuredContent": metadata, "isError": False}
                else:
                    result = {"content": [{"type": "text", "text": encoded(data).decode()}],
                              "structuredContent": data, "isError": False}
            elif method == "events/list" and modern:
                exact(params, set())
                result = {"events": [BRIDGE_EVENT_DEFINITION]}
            elif method == "events/subscribe" and modern:
                result = self.subscribe(principal, params)
            elif method == "events/unsubscribe" and modern:
                result = self.unsubscribe(principal, params)
            else:
                return self.json({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": "Method not found"}})
        except BridgeError as failure:
            if method == "tools/call":
                result = {"content": [{"type": "text", "text": failure.code}],
                          "structuredContent": dict({"error": failure.code}, **(failure.data or {})),
                          "isError": True}
            elif method.startswith("events/"):
                return self.json({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": failure.rpc if failure.rpc is not None else -32603,
                                            "message": "Event channel error",
                                            "data": dict({"reason": failure.code}, **(failure.data or {}))}})
            else:
                return self.json({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32602, "message": "Invalid parameters"}})
        if modern:
            result["resultType"] = "complete"
            result["_meta"] = {"io.modelcontextprotocol/serverInfo": BRIDGE_SERVER_INFO}
        return self.json({"jsonrpc": "2.0", "id": rid, "result": result})

    def subscribe(self, principal, params):
        exact(params, {"name", "delivery"}, {"arguments", "ttlMs", "cursor"})
        if params["name"] != BRIDGE_EVENT_NAME:
            raise BridgeError("unknown_event", rpc=-32011)
        if "cursor" in params and params["cursor"] is not None:
            # This event type carries cursor: null only; replay stays on the pull path.
            raise BridgeError("replay_cursor_unsupported", rpc=-32602)
        arguments, binding_generation = self.event_arguments(params, principal)
        delivery = params["delivery"]
        exact(delivery, {"mode", "url", "secret"})
        if delivery["mode"] != "webhook":
            raise BridgeError("delivery_mode_unsupported", rpc=-32602)
        url = text(delivery["url"], 512)
        parsed = urlsplit(url)
        if not (parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment):
            raise BridgeError("invalid_webhook_url", rpc=-32602)
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            raise BridgeError("invalid_webhook_url", rpc=-32602)
        if not (type(delivery["secret"]) is str and delivery["secret"].startswith("whsec_")
                and re.fullmatch(r"whsec_[A-Za-z0-9+/=]{32,128}", delivery["secret"]) is not None):
            raise BridgeError("invalid_webhook_secret", rpc=-32602)
        try:
            key = base64.b64decode(delivery["secret"][6:], validate=True)
        except (ValueError, binascii.Error):
            raise BridgeError("invalid_webhook_secret", rpc=-32602) from None
        if not 24 <= len(key) <= 64:
            raise BridgeError("invalid_webhook_secret", rpc=-32602)
        ttl_ms = params.get("ttlMs")
        if not (ttl_ms is None or type(ttl_ms) is int and ttl_ms > 0):
            raise BridgeError("invalid_ttl", rpc=-32602)
        if ttl_ms is None:
            ttl_ms = BRIDGE_EVENT_TTL
        ttl_ms = min(max(ttl_ms, BRIDGE_EVENT_TTL_MIN), BRIDGE_EVENT_TTL_MAX)
        args_json = json.dumps(dict(sorted(arguments.items())), separators=(",", ":"))
        identifier = self.store.subscription_identity(principal["id"], url, BRIDGE_EVENT_NAME, args_json)
        existing = self.store.find_subscription(principal["id"], url, BRIDGE_EVENT_NAME, args_json)
        if (existing is None or existing["expires"] <= self.store.clock()) \
                and self.store.count_subscriptions(principal["id"]) >= BRIDGE_EVENT_SUBSCRIPTION_LIMIT:
            raise BridgeError("subscription_limit", rpc=-32013,
                              data={"limit": "subscriptions", "max": BRIDGE_EVENT_SUBSCRIPTION_LIMIT})
        if not self.verify_callback(identifier, delivery["secret"], url, key):
            raise BridgeError("callback_verify_failed", rpc=-32015)
        expires = self.store.clock() + ttl_ms / 1000
        identifier = self.store.store_subscription(principal["id"], url, BRIDGE_EVENT_NAME,
                                                   args_json, delivery["secret"], expires, bound=True,
                                                   binding_generation=binding_generation)
        return {"id": identifier, "refreshBefore": iso_utc(expires), "cursor": None, "truncated": False}

    def unsubscribe(self, principal, params):
        exact(params, {"name", "delivery"}, {"arguments"})
        if params["name"] != BRIDGE_EVENT_NAME:
            raise BridgeError("unknown_event", rpc=-32011)
        arguments, binding_generation = self.event_arguments(params, principal)
        delivery = params["delivery"]
        exact(delivery, {"mode", "url"})
        check(delivery["mode"] == "webhook", "delivery_mode_unsupported")
        url = text(delivery["url"], 512)
        args_json = json.dumps(dict(sorted(arguments.items())), separators=(",", ":"))
        identifier = self.store.subscription_identity(principal["id"], url, BRIDGE_EVENT_NAME, args_json)
        with self.store.lock:
            self.store.require_chat_binding_generation(binding_generation)
            self.store.delete_subscription(principal["id"], identifier)
        return {}

    def event_arguments(self, params, principal):
        arguments = params.get("arguments", {})
        if type(arguments) is not dict:
            self.store.authorize_chat_binding(principal["id"], None)
        generation = self.store.authorize_chat_binding(principal["id"], arguments.get("conversation"))
        # The code is authorization, never a persisted filter or event field.
        arguments = {key: value for key, value in arguments.items() if key != "conversation"}
        exact(arguments, set(), {"submission_id"})
        if "submission_id" in arguments:
            opaque(arguments["submission_id"])
        return arguments, generation

    def verify_callback(self, identifier, secret, url, key):
        # A recent successful challenge by this exact principal/URL/secret may be reused.
        digest = hashlib.sha256(key).hexdigest()
        with self.jobs_cv:
            cached = self.verified.get(identifier)
            if cached and cached[1] == digest and time.monotonic() < cached[0]:
                return True
        nonce = secrets.token_urlsafe(24)
        try:
            status, raw = self.webhook_call({"id": identifier, "url": url,
                                             "secret": "whsec_" + base64.b64encode(key).decode("ascii"),
                                             "webhook_id": "msg_verification_" + secrets.token_hex(6)},
                                            encoded({"type": "verification", "challenge": nonce}))
        except WebhookFailure as failure:
            raise BridgeError("callback_" + failure.reason, rpc=-32015,
                              data={"reason": failure.reason}) from None
        match = False
        if 200 <= (status or 0) < 300:
            try:
                value = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                value = None
            match = type(value) is dict and type(value.get("challenge")) is str \
                and hmac.compare_digest(value["challenge"], nonce)
        if not match:
            raise BridgeError("callback_challenge_failed", rpc=-32015,
                              data={"reason": "challenge_failed"})
        with self.jobs_cv:
            if len(self.verified) > 128:
                self.verified.clear()
            self.verified[identifier] = (time.monotonic() + BRIDGE_WEBHOOK_VERIFICATION_TTL, digest)
        return True

    def webhook_call(self, subscription, body, webhook_id=None):
        """Signed HTTPS POST with an SSRF-checked fixed destination; returns (status, body)."""
        parsed = urlsplit(subscription["url"])
        hostname, port = parsed.hostname, parsed.port or 443
        target = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        # The webhook-id is the unique message identifier (eventId for event
        # deliveries, msg_* for control envelopes) — never the subscription id.
        headers = standard_webhook_headers(subscription["id"],
                                           webhook_id or subscription.get("webhook_id")
                                           or subscription.get("eventId") or subscription["id"],
                                           subscription["secret"], body)
        headers["X-MCP-Subscription-Id"] = subscription["id"]
        authority = "[" + hostname + "]" if ":" in hostname else hostname.encode("idna").decode("ascii")
        if parsed.port is not None:
            authority += ":" + str(port)
        headers["Host"] = authority
        try:
            addresses = webhook_addresses(hostname, port)
        except OSError:
            raise WebhookFailure("connection_refused") from None
        # Every resolved address must be public; mixed public/private DNS is rejected
        # so a private address can never be picked for delivery.
        if not addresses or any(not webhook_address_allowed(address) for address in addresses):
            raise WebhookFailure("connection_refused", retryable=False)
        connection = None
        try:
            connection = WebhookConnection(addresses[0], hostname, port)
            connection.connect()
            if connection.sock.getpeername()[0] not in addresses:
                raise WebhookFailure("connection_refused", retryable=False)
            connection.request("POST", target, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(BRIDGE_BODY_LIMIT + 1)
            if 200 <= response.status < 300:
                return response.status, raw
            raise WebhookFailure("http_4xx" if 400 <= response.status < 500 else "http_5xx",
                                 retryable=response.status not in {410, 413})
        except (WebhookFailure,):
            raise
        except (ssl.SSLError,):
            raise WebhookFailure("tls_error") from None
        except TimeoutError:
            raise WebhookFailure("timeout") from None
        except OSError:
            raise WebhookFailure("connection_refused") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def queue_output_events(self, output_id, submission_id, expires_at):
        """Called synchronously after the approved output row exists; delivery stays asynchronous."""
        event = {"eventId": output_id, "name": BRIDGE_EVENT_NAME,
                 "timestamp": iso_utc(self.store.clock()),
                 "data": {"submission_id": submission_id, "output_id": output_id, "expires_at": expires_at},
                 "cursor": None}
        for subscription in self.store.matching_subscriptions(submission_id, self.store.clock()):
            with self.jobs_cv:
                if len(self.jobs) < self.WEBHOOK_JOB_LIMIT:
                    self.jobs.append({"subscription": dict(subscription), "event": event,
                                      "attempts": 0, "ready": 0})
                    self.jobs_cv.notify_all()

    def webhook_loop(self):
        while not self.stopped:
            job = None
            with self.jobs_cv:
                now = time.monotonic()
                for index, item in enumerate(self.jobs):
                    if item["ready"] <= now:
                        job = self.jobs.pop(index)
                        break
                if job is None:
                    soonest = min((item["ready"] for item in self.jobs), default=None)
                    self.jobs_cv.wait(min(soonest - now if soonest is not None else 1.0, 1.0))
                    continue
            try:
                self.deliver(job)
            except Exception:
                pass  # A broken delivery never blocks serving; pull remains available.

    def deliver(self, job):
        # Delivery revalidates the subscription: unsubscribed, expired or revoked
        # principals do not start a send. An in-flight send cannot be recalled;
        # its ACK must pass a second atomic authorization check.
        current = self.store.active_subscription(job["subscription"]["id"], self.store.clock())
        expected_generation = job["subscription"]["binding_generation"]
        if current is None or current["binding_generation"] != expected_generation:
            return
        subscription, event = current, job["event"]
        status, failure = None, None
        try:
            status, _ = self.webhook_call(subscription, encoded(event),
                                          webhook_id=event["eventId"])
        except WebhookFailure as exc:
            failure = exc
        if failure is None and 200 <= (status or 0) < 300:
            self.store.mark_delivered(event["eventId"], subscription["id"], expected_generation)
            return
        if failure is not None and not failure.retryable:
            return  # Pull remains available; the subscription is not retried for this event.
        attempts = job["attempts"]
        if attempts >= self.WEBHOOK_ATTEMPT_LIMIT - 1:
            return
        with self.jobs_cv:
            if len(self.jobs) < self.WEBHOOK_JOB_LIMIT:
                delay = self.WEBHOOK_RETRY_DELAYS[min(attempts, len(self.WEBHOOK_RETRY_DELAYS) - 1)]
                self.jobs.append({"subscription": dict(job["subscription"]), "event": event,
                                  "attempts": attempts + 1, "ready": time.monotonic() + delay})

def bridge_certificate(directory, names, renew=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    directory = protected_directory(directory)
    key_path, cert_path = directory / "tls-key.pem", directory / "tls-cert.pem"
    check(key_path.exists() == cert_path.exists(), "incomplete_tls_identity")
    if not key_path.exists() or renew:
        if key_path.exists():
            key = serialization.load_pem_private_key(private_read(key_path), password=None)
            check(isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1), "tls_key_type")
        else:
            key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Admin in the Loop Bridge")])
        sans = []
        for name in names:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                check(re.fullmatch(r"[A-Za-z0-9.-]{1,253}", name) is not None, "invalid_tls_name")
                sans.append(x509.DNSName(name.lower()))
        check(bool(sans), "tls_names_required")
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=365)).add_extension(x509.SubjectAlternativeName(sans), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
                .sign(key, hashes.SHA256()))
        if not key_path.exists():
            private_write(key_path, key.private_bytes(serialization.Encoding.PEM,
                                                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    key_raw, cert_raw = private_read(key_path), private_read(cert_path)
    key = serialization.load_pem_private_key(key_raw, password=None)
    cert = x509.load_pem_x509_certificate(cert_raw)
    pub = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    check(key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == pub,
          "tls_key_mismatch")
    pin = hashlib.sha256(pub).hexdigest()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, pin


def bridge_serve(store, bind="127.0.0.1", port=0, names=(), ready_fd=None, bootstrap=None, pair=False):
    service = BridgeService(store)
    tls, pin = bridge_certificate(store.directory, names) if names else (None, None)
    check(ipaddress.ip_address(bind).is_loopback or tls is not None, "remote_requires_tls")

    def handler(kind):
        return type("_BridgeHandler" + kind, (BridgeRequestHandler,), {"service": service, "kind": kind})
    controller = BridgeHTTPServer((bind, port), handler("CONTROLLER"), tls)
    mcp = BridgeHTTPServer(("127.0.0.1", 0), handler("MCP"))
    assigned, mcp_port = controller.socket.getsockname()[1], mcp.socket.getsockname()[1]
    # Set mutable host allowlists only after the OS has assigned the random port.
    service.controller_hosts.update(f"{name}:{assigned}" for name in names or (bind,))
    if ":" in bind:
        service.controller_hosts.add(f"[{bind}]:{assigned}")
    service.mcp_hosts.add(f"127.0.0.1:{mcp_port}")
    stop = threading.Event()

    def cleanup():
        while not stop.is_set():
            if stop.wait(30):
                return
            store.cleanup()

    def signal_stop(_signum, _frame):
        stop.set()

    cleanup_thread = threading.Thread(target=cleanup, name="aitl-cleanup", daemon=True)
    cleanup_thread.start()
    previous_handlers = {}
    for value in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[value] = signal.signal(value, signal_stop)
    servers = (controller, mcp)
    loops = []
    try:
        service.start()
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, name="aitl-serve", daemon=True)
            thread.start()
            loops.append(thread)
        info = {"version": 1, "port": assigned, "mcp_port": mcp_port,
                "instance": store.instance, "conversation": store.conversation, "pin": pin}
        if ready_fd is not None:
            os.write(ready_fd, encoded(info) + b"\n")
            os.close(ready_fd)
        else:
            # Operator console is explicit credential setup, not an application log.
            print(json.dumps(info), flush=True)
            if pair:
                print("PAIR CODE (2 minutes): " + store.open_pair(), flush=True)
        stop.wait()
    finally:
        service.stop()
        for server in servers:
            server.shutdown()
            server.server_close()
        service.wait_idle()
        for value, handler_function in previous_handlers.items():
            signal.signal(value, handler_function)


def bridge_main(argv=None):
    os.umask(0o077)
    # Include parser and access logs: hostile paths/body errors must not reach logs.
    logging.disable(logging.CRITICAL)
    parser = BridgeArgumentParser(description="Admin in the Loop — transport-only Bridge/MCP service")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--conversation", default=None)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--tls-name", action="append", default=[])
    parser.add_argument("--pair", action="store_true", help="Open a local two-minute pairing window on start")
    parser.add_argument("--revoke", metavar="CONTROLLER_ID")
    parser.add_argument("--renew-cert", action="store_true", help="Renew TLS certificate locally, preserving its SPKI key")
    parser.add_argument("--bootstrap-fd", type=int)
    parser.add_argument("--ready-fd", type=int)
    args = parser.parse_args(argv)
    directory = protected_directory(args.state_dir)
    conversation_path = directory / "conversation"
    if conversation_path.exists():
        conversation = private_read(conversation_path, 64).decode("ascii")
        check(args.conversation is None or args.conversation == conversation, "conversation_state_mismatch")
    else:
        conversation = args.conversation or secrets.token_hex(32)
        opaque(conversation)
        private_write(conversation_path, conversation.encode("ascii"))
    store = BridgeStore(directory, conversation)
    try:
        if args.renew_cert:
            check(bool(args.tls_name) and args.bootstrap_fd is None, "tls_names_required")
            _, pin = bridge_certificate(directory, args.tls_name, renew=True)
            print("Renewed SPKI SHA-256: " + pin)
            return 0
        if args.revoke:
            store.revoke(args.revoke)
            print("Controller token revoked.")
            return 0
        if args.bootstrap_fd is not None:
            with os.fdopen(args.bootstrap_fd, "rb") as source:
                boot = parse(source.read(4097))
            exact(boot, {"controller_token", "ai_token", "conversation"})
            check(boot["conversation"] == conversation and args.ready_fd is not None and not args.tls_name
                  and args.bind == "127.0.0.1", "invalid_bootstrap")
            store.issue("CONTROLLER", boot["controller_token"])
            store.issue("AI", boot["ai_token"])
        else:
            ai_file = directory / "ai-token"
            if not ai_file.exists():
                ai, _ = store.issue("AI")
                private_write(ai_file, ai.encode())
            else:
                store.authenticate(private_read(ai_file, 64).decode(), "AI")
        check(0 <= args.port <= 65535, "invalid_port")
        bridge_serve(store, args.bind, args.port, args.tls_name, args.ready_fd, pair=args.pair)
        return 0
    finally:
        store.close()


## END AITL EMBEDDED BRIDGE SOURCE


_bridge_components = _load_bridge_component()
# The exec namespace is the block's own globals; exposing it lets tests patch
# transport-level hooks (e.g. webhook address resolution) deterministically.
_bridge_component = _bridge_components
for _bridge_name in ("BridgeError", "BridgeStore", "BridgeService", "BRIDGE_BODY_LIMIT",
                     "BRIDGE_RETENTION", "BRIDGE_OUTPUT_LIMIT", "BRIDGE_BLOCK_SIZES",
                     "BRIDGE_VERSIONS", "BRIDGE_TOOLS", "BRIDGE_CHAT_ALPHABET", "BRIDGE_CHAT_LIFETIME",
                     "BRIDGE_CHAT_CODE_SCHEMA", "new_chat_code", "normalize_chat_code",
                     "bridge_certificate", "bridge_serve",
                     "bridge_main", "parse", "encoded", "WebhookFailure",
                     "BRIDGE_EVENT_NAME", "BRIDGE_EVENT_DEFINITION", "WebhookConnection",
                     "standard_webhook_headers", "webhook_address_allowed"):
    globals()[_bridge_name] = _bridge_components[_bridge_name]


def main():
    if sys.version_info < (3, 11) or os.name != "posix":
        print("Requires Unix and Python >= 3.11.", file=sys.stderr)
        return 2
    if len(sys.argv) == 4 and sys.argv[1] == "--_shell-child":
        shell_child(int(sys.argv[2]), sys.argv[3])
        return 2
    if len(sys.argv) == 4 and sys.argv[1] == "--_outer-test-child":
        outer_test_child(int(sys.argv[2]), sys.argv[3])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--bridge":
        return bridge_main(sys.argv[2:])
    if "--bridge" in sys.argv[1:]:
        raise ProbeError("choose_one_role")
    parser = SafeArgumentParser(description="Admin in the Loop — human-controlled MVP")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--bridge", action="store_true", help="Start the headless transport-only Bridge service")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--terminal-helper", metavar="SESSION_DIRECTORY")
    args = parser.parse_args()
    require(sum((args.gui, args.bridge, args.self_test, args.terminal_helper is not None)) <= 1, "choose_one_role")
    if args.self_test:
        return run_all_tests()
    if args.terminal_helper:
        run_terminal_helper(args.terminal_helper)
        return 0
    if args.gui or not (args.self_test or args.terminal_helper is not None):
        run_gui()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BridgeError as exc:
        print("Stopped: " + exc.code, file=sys.stderr)
        sys.exit(2)
    except ProbeError as exc:
        print("Stopped: " + str(exc), file=sys.stderr)
        sys.exit(2)
    except Exception:
        print("Stopped: local runtime error (content omitted).", file=sys.stderr)
        sys.exit(2)
