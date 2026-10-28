# Core stall stack capture

This temporary diagnostic preserves the stacks that ordinary Profiler cannot capture while Core's event loop is stuck. It does not restart Core or repair the cause.

After Home Assistant starts, a heartbeat rearms Python's native 15-second traceback watchdog every five seconds. If the loop stops making progress, the native watchdog writes all Python thread stacks to `/config/core-stall-watchdog.log`. It writes once per continuous stall. A recovered heartbeat records the UTC interval and rearms it.

The component stops arming new dumps when the file reaches 5 MiB. One in-flight dump can exceed that limit. The file is private runtime data and is ignored by git. Healthy heartbeats do not write to disk. Each rearm replaces a native watchdog thread, so this diagnostic has a measurable overhead.

This component owns the process-global `faulthandler.dump_traceback_later` timer. Do not run another owner of that timer concurrently. It leaves Home Assistant's separate crash handler enabled. Normal shutdown cancels the timer before closing the file.

Use the captured function and its callers to investigate the stall. Preserve the dump before another restart. Remove this component and its YAML entry after the cause is fixed, then restart Core to unload the diagnostic.
