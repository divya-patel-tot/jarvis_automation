"""Windows Job Object wrapper (ctypes, no pywin32 dependency).

Every agent process tree is placed in a job created with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, so terminating the job (or the job handle
being closed when Jarvis exits/crashes) kills every descendant: the bridge,
the shells it spawns, scripts, helpers. This is what makes the kill switch
reliable (plan section 6.4 / 10.2.10).

On non-Windows platforms the class degrades to a no-op tracker so unit tests
can import it, but Jarvis itself only supports Windows.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JobObjectExtendedLimitInformation = 9
JobObjectBasicProcessIdList = 3
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]


class JobObject:
    """A kill-on-close job. Use one per supervised process tree."""

    def __init__(self, name: str | None = None) -> None:
        self._handle = None
        self._pids: list[int] = []
        if not IS_WINDOWS:
            return
        handle = _k32.CreateJobObjectW(None, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            err = ctypes.get_last_error()
            _k32.CloseHandle(handle)
            raise ctypes.WinError(err)
        self._handle = handle

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign_pid(self, pid: int) -> bool:
        self._pids.append(pid)
        if not IS_WINDOWS or self._handle is None:
            return False
        hproc = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not hproc:
            log.warning("OpenProcess(%s) failed: %s", pid, ctypes.get_last_error())
            return False
        try:
            if not _k32.AssignProcessToJobObject(self._handle, hproc):
                log.warning("AssignProcessToJobObject(%s) failed: %s", pid, ctypes.get_last_error())
                return False
            return True
        finally:
            _k32.CloseHandle(hproc)

    def assign(self, proc: subprocess.Popen) -> bool:
        return self.assign_pid(proc.pid)

    def process_ids(self) -> list[int]:
        """PIDs currently in the job (Windows), else the PIDs assigned."""
        if not IS_WINDOWS or self._handle is None:
            return list(self._pids)
        # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORDs then ULONG_PTR array.
        cap = 1024
        buf_t = ctypes.c_byte * (8 + ctypes.sizeof(ctypes.c_size_t) * cap)
        buf = buf_t()
        ret = wintypes.DWORD(0)
        if not _k32.QueryInformationJobObject(
                self._handle, JobObjectBasicProcessIdList, buf, ctypes.sizeof(buf), ctypes.byref(ret)):
            return list(self._pids)
        count = ctypes.cast(ctypes.byref(buf, 4), ctypes.POINTER(wintypes.DWORD)).contents.value
        arr = ctypes.cast(ctypes.byref(buf, 8), ctypes.POINTER(ctypes.c_size_t * cap)).contents
        return [int(arr[i]) for i in range(min(count, cap))]

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job immediately."""
        if IS_WINDOWS and self._handle is not None:
            if not _k32.TerminateJobObject(self._handle, exit_code):
                log.warning("TerminateJobObject failed: %s", ctypes.get_last_error())
        else:  # best effort fallback
            _kill_pids_psutil(self._pids)

    def close(self) -> None:
        """Close the handle. Because of KILL_ON_JOB_CLOSE this kills the tree."""
        if IS_WINDOWS and self._handle is not None:
            _k32.CloseHandle(self._handle)
        self._handle = None

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:
            pass


def _kill_pids_psutil(pids: list[int]) -> None:
    try:
        import psutil
    except ImportError:
        return
    for pid in pids:
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.Error:
            pass


def kill_tree(pid: int) -> None:
    """Kill a process and all its descendants (used when no job is available)."""
    _kill_pids_psutil([pid])
