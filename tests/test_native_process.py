"""Exercise native ownership with bounded synthetic processes, without network."""

import asyncio
import errno
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from msu_hub_bot.execution import process as native
from msu_hub_bot.execution.executor import TPExecutor


def test_captured_output_is_bounded_and_diagnostics_are_discarded():
    command = [sys.executable, "-c", "import sys; sys.stderr.write('private diagnostic'); sys.stdout.write('hello')"]
    assert native.run_process(command, timeout=3, max_output_bytes=5) == b"hello"
    with pytest.raises(native.ProcessOutputTooLarge, match="size limit"):
        native.run_process(command, timeout=3, max_output_bytes=4)
    command[-1] += "; sys.exit(2)"
    with pytest.raises(subprocess.CalledProcessError) as failure:
        native.run_process(command, timeout=3, max_output_bytes=10)
    assert failure.value.stdout is None and failure.value.stderr is None


def test_exiting_group_permission_race_preserves_the_output_limit(monkeypatch):
    popen, killpg = subprocess.Popen, os.killpg
    children = []
    signals = []

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        child.wait(timeout=3)
        # Darwin can deny the signal before a nonblocking poll observes exit.
        monkeypatch.setattr(child, "poll", lambda: None)
        children.append(child)
        return child

    def deny_once(pid, sig):
        signals.append((pid, sig))
        if len(signals) == 1:
            raise PermissionError(errno.EPERM, "synthetic exiting group")
        return killpg(pid, sig)

    monkeypatch.setattr(native.subprocess, "Popen", capture)
    monkeypatch.setattr(native.os, "killpg", deny_once)
    with pytest.raises(native.ProcessOutputTooLarge):
        native.run_process([sys.executable, "-c", "print('hello')"], timeout=3, max_output_bytes=4)
    assert signals == [(children[0].pid, signal.SIGKILL)] * 2
    assert children[0].returncode == 0 and children[0].stdout.closed


@pytest.mark.parametrize("leader_exited", [False, True])
def test_genuine_group_permission_errors_remain_visible_and_waits_are_bounded(monkeypatch, leader_exited):
    denied = PermissionError(errno.EPERM, "synthetic live group denial")
    wait = Mock(return_value=0) if leader_exited else Mock(side_effect=subprocess.TimeoutExpired(["synthetic"], 1))
    child = SimpleNamespace(pid=123456, stdout=None, returncode=0 if leader_exited else None, wait=wait)
    killpg = Mock(side_effect=denied)
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(native.os, "killpg", killpg)
    with pytest.raises(PermissionError) as failure:
        native.run_process(["synthetic"], timeout=3)
    assert failure.value is denied
    assert killpg.call_count == (2 if leader_exited else 1)
    assert all(0 < call.kwargs["timeout"] <= 3 for call in wait.call_args_list)


def test_successful_group_signal_does_not_hide_a_stalled_reap(monkeypatch):
    cleanup_timeout = subprocess.TimeoutExpired(["synthetic"], 1)
    wait = Mock(side_effect=[subprocess.TimeoutExpired(["synthetic"], 3), cleanup_timeout])
    child = SimpleNamespace(pid=123456, stdout=None, returncode=None, wait=wait)
    killpg = Mock()
    monkeypatch.setattr(native.subprocess, "Popen", Mock(return_value=child))
    monkeypatch.setattr(native.os, "killpg", killpg)
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        native.run_process(["synthetic"], timeout=3)
    assert failure.value is cleanup_timeout
    killpg.assert_called_once_with(child.pid, signal.SIGKILL)
    assert all(0 < call.kwargs["timeout"] <= 3 for call in wait.call_args_list)


@pytest.mark.parametrize("limit", [0, 65536 * 2])
def test_large_output_does_not_deadlock(limit):
    result = native.run_process([sys.executable, "-c", "import sys; sys.stdout.write('x' * 131072)"], timeout=3, max_output_bytes=limit)
    assert len(result) == limit


def test_inherited_stdout_cannot_keep_a_job_running_past_its_deadline():
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])",
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        native.run_process(command, timeout=0.5, max_output_bytes=1024)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadlines_never_launch_a_process(timeout):
    with pytest.raises(ValueError, match="timeout"):
        native.run_process(["missing-synthetic-program"], timeout=timeout)


@pytest.mark.parametrize("finish", ["timeout", "success", "retry_after_leader_exit"])
def test_process_groups_stop_descendants_and_reap_direct_children(monkeypatch, tmp_path, finish):
    popen = subprocess.Popen
    children = []
    descendant_id = tmp_path / "descendant.pid"
    script = tmp_path / "tree.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
        "Path(sys.argv[1]).write_text(str(child.pid))\n"
        "if sys.argv[2] == 'timeout': time.sleep(5)\n"
    )

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(native.subprocess, "Popen", capture)
    signals = []
    if finish == "retry_after_leader_exit":
        killpg = os.killpg

        def deny_once(pid, sig):
            signals.append((pid, sig))
            if len(signals) == 1:
                raise PermissionError(errno.EPERM, "synthetic exiting leader")
            return killpg(pid, sig)

        monkeypatch.setattr(native.os, "killpg", deny_once)
    command = [sys.executable, str(script), str(descendant_id), finish]
    if finish == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            native.run_process(command, timeout=0.5)
        assert children[0].returncode == -signal.SIGKILL
    else:
        assert native.run_process(command, timeout=3) == b""
        assert children[0].returncode == 0
    if finish == "retry_after_leader_exit":
        assert signals == [(children[0].pid, signal.SIGKILL)] * 2
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    descendant = int(descendant_id.read_text())
    deadline = time.monotonic() + 2
    while True:
        # A terminated orphan can briefly await the OS reaper on Linux.
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)], capture_output=True, text=True, check=False).stdout.strip()
        if not status or status.startswith("Z"):
            break
        assert time.monotonic() < deadline, "native descendant survived process-group cleanup"
        time.sleep(0.01)


@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
async def test_native_deadline_recovers_a_worker_after_caller_abandonment(monkeypatch, abandon):
    popen = subprocess.Popen
    started = threading.Event()
    children = []

    def capture(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(native.subprocess, "Popen", capture)
    worker = TPExecutor(1)

    def convert():
        try:
            native.run_process([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        except subprocess.TimeoutExpired:
            return None

    request = asyncio.create_task(worker.run(convert, timeout=0.15 if abandon == "timeout" else None))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        if abandon == "cancel":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            assert await request == (None, True)
        assert worker._running == 1
        assert await worker.run(lambda: "recovered", timeout=2) == ("recovered", False)
        assert children[0].returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
    finally:
        await asyncio.gather(request, return_exceptions=True)
        worker.shutdown(wait=True)
