"""Run local tools with bounded output and a killable process group."""

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress


class ProcessOutputTooLarge(ValueError):
    """The native tool exceeded its allowed stdout size."""


def run_process(command: Sequence[str], *, timeout: float, max_output_bytes: int = 0) -> bytes:
    """Discard diagnostics, bound runtime/output, and reap the direct child.

    POSIX process groups also stop descendants holding files or pipes open.
    Cancelling a thread's caller cannot interrupt native work; this deadline
    remains effective until that thread releases its executor slot.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes must not be negative")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if max_output_bytes else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()
    try:
        if process.stdout is not None:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(command, timeout)
                    chunk = os.read(process.stdout.fileno(), min(65536, max_output_bytes - len(output) + 1))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > max_output_bytes:
                        raise ProcessOutputTooLarge("Native output exceeded its size limit")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        return bytes(output)
    finally:
        try:
            # Also remove descendants after a launcher exits successfully.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        finally:
            try:
                process.wait()
            finally:
                if process.stdout is not None:
                    process.stdout.close()
