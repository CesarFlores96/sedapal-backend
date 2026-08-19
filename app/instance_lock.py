import os
import sys
from pathlib import Path

_lock_file = None


def acquire_instance_lock() -> bool:
    """
    Asegura que solo una instancia de FastAPI pueda correr a la vez.
    Si ya hay otra instancia ejecutándose en el sistema, impide que la segunda arranque.
    """
    global _lock_file
    lock_file_path = Path(__file__).resolve().parent.parent / ".fastapi_instance.lock"

    try:
        _lock_file = open(lock_file_path, "a+b")
        _lock_file.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(f"PID: {os.getpid()}\n".encode("utf-8"))
        _lock_file.flush()
        return True
    except (BlockingIOError, PermissionError, OSError):
        print(f"\n=================================================================")
        print(f"[BLOQUEO] Ya existe otra instancia de FastAPI ejecutándose.")
        print(f"[BLOQUEO] No se permite ejecutar 2 instancias simultáneamente.")
        print(f"=================================================================\n")
        sys.exit(1)


def release_instance_lock() -> None:
    global _lock_file
    if _lock_file:
        try:
            if sys.platform == "win32":
                import msvcrt

                _lock_file.seek(0)
                msvcrt.locking(_lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_lock_file.fileno(), fcntl.LOCK_UN)
            _lock_file.close()
        except Exception:
            pass
        _lock_file = None
