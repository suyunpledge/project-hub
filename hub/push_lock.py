"""An OS lock shared by Hub and the Git process that can still update the remote."""
import os
import subprocess
import time
from contextlib import contextmanager


class PushBusy(Exception):
    pass


def _open_locked(path):
    if os.name == "nt":
        import _winapi
        import msvcrt
        # OPEN_ALWAYS, no file sharing. Duplicated handles share the open file object,
        # unlike a Windows byte-range lock, which dies with the process that locked it.
        handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
                                    0, 0, 4, 0x80, 0)
        return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDWR), "r+b")
    import fcntl
    stream = path.open("a+b")
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise
    return stream


@contextmanager
def acquire(path, wait):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 15
    while True:
        try:
            stream = _open_locked(path)
            break
        except OSError as exc:
            if not isinstance(exc, BlockingIOError) and getattr(exc, "winerror", None) != 32:
                raise
            if not wait or time.monotonic() >= deadline:
                raise PushBusy from exc
            time.sleep(0.05)
    with stream:
        yield stream.fileno()


@contextmanager
def child_options(fd):
    if fd is None:
        yield {}
    elif os.name != "nt":
        yield {"pass_fds": (fd,)}
    else:
        import msvcrt
        with os.fdopen(os.dup(fd), "rb") as copy:
            handle = msvcrt.get_osfhandle(copy.fileno())
            os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
            yield {"startupinfo": startup, "close_fds": True}
