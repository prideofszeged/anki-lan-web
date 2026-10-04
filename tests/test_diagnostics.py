"""A wedged server must be diagnosable without py-spy/ptrace: SIGUSR1 dumps every thread's stack."""
import subprocess
import sys
import textwrap

SNIPPET = textwrap.dedent("""
    import os, signal, sys
    from ankiweb.__main__ import enable_diagnostics
    enable_diagnostics()
    def busy_marker_function():
        os.kill(os.getpid(), signal.SIGUSR1)   # dump while "inside" application code
    busy_marker_function()
    print("still-alive", flush=True)
""")


def test_sigusr1_dumps_all_thread_stacks_and_process_survives():
    done = subprocess.run([sys.executable, "-c", SNIPPET], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert "still-alive" in done.stdout                      # the signal must not kill the server
    assert "most recent call first" in done.stderr           # faulthandler traceback header
    assert "busy_marker_function" in done.stderr             # and it names where we were
