"""Child processes that can be ended with everything they started: a worker's tools and a test command's servers
must not outlive a stop.

On POSIX the child leads a new session (its own process group), and `kill_tree` signals the whole group, including
processes whose parent already exited. On Windows the child gets a new process group and `kill_tree` runs
`taskkill /F /T`, which walks the live process tree: a child whose parent already exited is no longer in it.
"""

import os
import signal
import subprocess

WINDOWS = os.name == 'nt'
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)


def group_options():
    """Keyword arguments for Popen or asyncio.create_subprocess_exec that start the child in its own group."""
    return {'creationflags': CREATE_NEW_PROCESS_GROUP} if WINDOWS else {'start_new_session': True}


def kill_tree(pid):
    """Ends the process `pid` started with group_options() and every process it started. Already gone is fine."""
    if WINDOWS:
        # The caller still holds the process handle, so the pid cannot be reused yet. Exit code 128 ("not found")
        # after a normal exit is expected.
        taskkill = os.path.join(os.environ.get('SystemRoot') or r'C:\Windows', 'System32', 'taskkill.exe')
        try:
            subprocess.run([taskkill, '/F', '/T', '/PID', str(pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
