"""Every subprocess the runner starts goes through this module.

It holds invariants 1 and 2 of the lifecycle (the full list is at the top of
`lifecycle.py`; the two are repeated here because this file is where they are
enforced):

1. A started process, its children and its grandchildren die when its run
   ends, whether it exited, timed out or the runner itself was interrupted.
   Windows: the process is created suspended, put in a Job Object that has
   KILL_ON_JOB_CLOSE and no breakaway, and only then resumed, so it cannot
   start a child that escapes the job. The job is terminated on every exit
   path, and closing its handle (also what happens when the runner dies) kills
   whatever is left. POSIX: the process leads a new session and the whole
   process group is sent SIGKILL on every exit path.
2. No wait can hang. Child output goes to files, never to a pipe, because a
   grandchild that inherits a pipe keeps it open and makes a read block long
   after the deadline. Standard input is a temporary file or the null device.
   Every wait takes a deadline, including the waits after a kill.

Known limits, stated so nobody mistakes this for a sandbox: a program that
leaves the job or the session by other means (a scheduled task, a service, a
desktop automation request handled by another process) is not reached, and
the killed processes run with the user's rights until the moment they die.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, Mapping, Sequence

KILL_REAP_SECONDS = 10.0
SETTLE_SECONDS = 5.0
POLL_SECONDS = 0.02
CAPTURE_LIMIT_BYTES = 64 * 1024


@dataclass
class Outcome:
    """What happened to one bounded process. Nothing in here is a path."""

    started: bool
    exit_status: int | None = None
    timed_out: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class _Group:
    """The set of processes one run owns. A subclass exists per platform."""

    def spawn(
        self,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        stdin: object,
        stdout: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    def alive(self) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        """Release the group. On Windows this also kills anything still in it."""

    def wait_empty(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while self.alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_SECONDS)
        return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    CREATE_SUSPENDED = 0x00000004
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _last_error(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    class _JobGroup(_Group):
        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise _last_error("CreateJobObjectW")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                error = _last_error("SetInformationJobObject")
                _kernel32.CloseHandle(handle)
                raise error
            self._job: int | None = handle

        def spawn(self, argv, cwd, env, stdin, stdout):  # type: ignore[no-untyped-def]
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_SUSPENDED,
            )
            try:
                handle = int(process._handle)  # type: ignore[attr-defined]
                if not _kernel32.AssignProcessToJobObject(self._job, handle):
                    raise _last_error("AssignProcessToJobObject")
                status = _ntdll.NtResumeProcess(handle)
                if status != 0:
                    raise OSError(f"NtResumeProcess failed with status {status}")
            except BaseException:
                process.kill()
                raise
            return process

        def kill(self) -> None:
            if self._job:
                _kernel32.TerminateJobObject(self._job, 1)

        def alive(self) -> bool:
            if not self._job:
                return False
            info = _Accounting()
            if not _kernel32.QueryInformationJobObject(
                self._job,
                JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                return False
            return info.ActiveProcesses > 0

        def close(self) -> None:
            job, self._job = self._job, None
            if job:
                _kernel32.CloseHandle(job)

    def _new_group() -> _Group:
        return _JobGroup()

else:

    class _SessionGroup(_Group):
        def __init__(self) -> None:
            self._pgid: int | None = None

        def spawn(self, argv, cwd, env, stdin, stdout):  # type: ignore[no-untyped-def]
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._pgid = process.pid
            return process

        def kill(self) -> None:
            if self._pgid is None:
                return
            try:
                os.killpg(self._pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        def alive(self) -> bool:
            if self._pgid is None:
                return False
            try:
                os.killpg(self._pgid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

    def _new_group() -> _Group:
        return _SessionGroup()


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    stdout: BinaryIO,
    deadline_seconds: float,
    stdin_bytes: bytes | None = None,
) -> Outcome:
    """Run a program to its end or its deadline, then kill everything it started.

    Never raises for a program that cannot start or will not stop; the outcome
    says what happened. Output (standard output and standard error together)
    goes to `stdout`, an open binary file.
    """
    try:
        group = _new_group()
    except Exception as error:  # fail closed: no group, no process
        return Outcome(started=False, error=f"Could not create a process group: {error}")
    stdin_file = None
    try:
        stdin: object = subprocess.DEVNULL
        if stdin_bytes is not None:
            stdin_file = tempfile.TemporaryFile()
            stdin_file.write(stdin_bytes)
            stdin_file.flush()
            stdin_file.seek(0)
            stdin = stdin_file
        try:
            process = group.spawn(argv, os.fspath(cwd), env, stdin, stdout)
        except (OSError, ValueError) as error:
            return Outcome(started=False, error=f"{type(error).__name__}: {error}")
        outcome = Outcome(started=True)
        try:
            try:
                process.wait(timeout=max(0.0, deadline_seconds))
            except subprocess.TimeoutExpired:
                outcome.timed_out = True
        finally:
            group.kill()
        try:
            code = process.wait(timeout=KILL_REAP_SECONDS)
        except subprocess.TimeoutExpired:
            code = None
            outcome.warnings.append("The process did not exit after it was stopped")
        if not outcome.timed_out:
            outcome.exit_status = code
        if not group.wait_empty(SETTLE_SECONDS):
            outcome.warnings.append("Some processes it started could not be confirmed stopped")
        return outcome
    finally:
        group.close()
        if stdin_file is not None:
            stdin_file.close()


def run_captured(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    deadline_seconds: float,
    keep: str = "head",
    limit: int = CAPTURE_LIMIT_BYTES,
) -> tuple[Outcome, str]:
    """Like `run_bounded`, returning the first or last `limit` bytes of the output as text."""
    with tempfile.TemporaryFile() as sink:
        outcome = run_bounded(
            argv, cwd=cwd, env=env, stdout=sink, deadline_seconds=deadline_seconds
        )
        size = sink.seek(0, os.SEEK_END)
        sink.seek(max(0, size - limit) if keep == "tail" else 0)
        data = sink.read(limit)
    return outcome, data.decode("utf-8", errors="replace")
