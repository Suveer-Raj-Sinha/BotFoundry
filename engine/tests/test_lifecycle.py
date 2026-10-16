import os
import subprocess
import sys
import threading

from botfoundry.lifecycle import is_process_alive, watch_parent


def test_current_process_is_alive():
    assert is_process_alive(os.getpid()) is True


def test_finished_process_is_not_alive():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert is_process_alive(proc.pid) is False


def test_watchdog_fires_when_process_exits():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"])
    # Reap the child as soon as it exits (otherwise POSIX keeps it around as a zombie).
    threading.Thread(target=proc.wait, daemon=True).start()
    fired = threading.Event()
    watch_parent(proc.pid, fired.set, interval=0.1)
    assert fired.wait(timeout=10), "watchdog did not notice the process exit"


def test_watchdog_stays_quiet_while_process_lives():
    fired = threading.Event()
    stop = threading.Event()
    watch_parent(os.getpid(), fired.set, interval=0.05, stop=stop)
    assert not fired.wait(timeout=0.4)
    stop.set()
