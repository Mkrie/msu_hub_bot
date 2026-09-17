import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.execution import sed


@pytest.mark.parametrize(
    "text, commands, expected",
    [
        ("Cat cat", ["s/cat/dog/i"], "dog dog"),
        ("Cat cat", ["s/cat/dog/"], "dog dog"),
        ("a\nb", ["s/^/!/m"], "!a\n!b"),
        ("a/b", [r"s/a\/b/c/"], "c"),
        ("before", [r"s/before/after\/path/"], "after/path"),
        ("ab", [r"s/(a)(b)/\2\1/"], "ba"),
        ("Привет", ["ы/привет/Пока/i"], "Пока"),
        ("aaa", ["s/a/b/", "s/b/c/"], "ccc"),
        ("a", ["s/a//"], ""),
        ("original", ["s/[/bad/"], "original"),
        ("original", ["s/original/bad/L"], "original"),
        ("original", ["not a substitution"], None),
    ],
)
def test_substitution_syntax_and_flags(text, commands, expected):
    assert sed.sed_calc(text, commands) == expected


def test_substitutions_have_bounded_output_and_count():
    assert len(sed.sed_calc("a" * 4096, ["s/a/" + "b" * 2000 + "/"])) == 4096
    assert sed.sed_calc("a", ["s/a/b/"] * 5 + ["s/b/c/"]) == "b"


@pytest.mark.parametrize("stop", ["regex_timeout", "caller_timeout", "cancel"])
def test_pathological_regex_is_killed_and_worker_slot_recovers(stop):
    # A separate interpreter can kill the entire probe even if a regex holds the GIL.
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), stop],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    ) as probe:
        try:
            output, _ = probe.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(probe.pid, signal.SIGKILL)
            output, _ = probe.communicate()
            pytest.fail(f"Regex probe exceeded the independent watchdog: {output}")
        finally:
            # Also terminate descendants if the probe exits before its own cleanup.
            with suppress(ProcessLookupError):
                os.killpg(probe.pid, signal.SIGKILL)
        assert probe.returncode == 0, output


async def _probe_pathological_regex(stop):
    children = []

    class TrackedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    async def wait_until(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.005)

    pulses = 0

    async def heartbeat():
        nonlocal pulses
        while True:
            pulses += 1
            await asyncio.sleep(0.01)

    executor = TPExecutor(1)
    ticker = asyncio.create_task(heartbeat())
    with patch.object(sed, "SED_TIMEOUT", 0.5), patch.object(sed.subprocess, "Popen", TrackedPopen):
        started = time.monotonic()
        task = asyncio.create_task(
            executor.run(sed.sed_calc, "a" * 1000 + "!", ["s/(a+)+$/x/"], timeout=0.1 if stop == "caller_timeout" else 3)
        )
        try:
            await wait_until(lambda: len(children) == 1)
            child = children[0]
            if stop == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif stop == "caller_timeout":
                assert await task == (None, True)
            else:
                with pytest.raises(sed.SedTimeout):
                    await task

            if stop != "regex_timeout":
                assert child.poll() is None
                assert executor._running == 1
                # Caller abandonment must not make room for another native process.
                assert await executor.run(lambda: "queued", timeout=0.01) == (None, True)

            await wait_until(lambda: executor._running == 0)
            assert time.monotonic() - started < 3
            assert pulses >= 5
            assert child.returncode == -signal.SIGKILL
            with pytest.raises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

            with patch.object(sed, "SED_TIMEOUT", 2):
                assert await executor.run(sed.sed_calc, "hello", ["s/hello/bye/"], timeout=3) == ("bye", False)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            executor.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(_probe_pathological_regex(sys.argv[1]))
