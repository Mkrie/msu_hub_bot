# Bounded execution

- [executor.py](executor.py) owns thread admission, deadlines, recovery and shutdown. Running work retains its slot after caller cancellation; every job must bound its own I/O and native processes.
- Follow [media execution](../../../docs/media-execution.md): admit before downloading, transfer immutable inputs, and use the shared process runner for native deadlines and cleanup.
- [sed.py](sed.py) evaluates user regexes in a disposable isolated process. Preserve input limits, timeout termination and operation from the installed package without checkout paths.
- Keep command formatting and FSM outside this layer. Export aggregate worker telemetry without request context, raw inputs or exception text.
