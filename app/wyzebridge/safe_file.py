"""Settings files that survive a power cut: written beside the real file, then swapped in whole."""

import json
import os
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from wyzebridge.logging import logger

BAK = ".bak"


def _swap(tmp: str, path: str) -> None:
    for attempt in range(5):
        try:
            return os.replace(tmp, path)
        except PermissionError:  # Windows: another thread is reading the file right now
            if attempt == 4:
                raise
            time.sleep(0.05)


@contextmanager
def writing(path, mode: str = "w", perms: int | None = None):
    """Like open(path, "w"), but the old file stays whole until the new one is fully on disk.
    The file it replaces is kept as <name>.bak, the last good copy for restore_broken()."""
    path = os.fspath(path)
    tmp = f"{path}.tmp"
    if perms is None:  # a file made private stays private
        perms = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o666
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, perms), mode) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(path) and os.path.getsize(path):
            try:
                shutil.copy2(path, path + BAK)
            except OSError:
                pass  # no last good copy this time; the save itself still goes through
        _swap(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text: str) -> None:
    with writing(path) as f:
        f.write(text)


def restore_broken(folder) -> list[str]:
    """At startup: put the last good copy back for any .json settings file that no longer reads. Returns the names restored."""
    restored = []
    for path in Path(folder).glob("*.json"):
        try:
            head = path.read_bytes()
            if head[:1] not in (b"{", b"[", b""):
                continue  # sealed by the vault, not plain JSON
            json.loads(head)
            continue
        except ValueError:
            pass
        except OSError:
            continue
        bak = Path(f"{path}{BAK}")
        try:
            json.loads(bak.read_bytes())
            shutil.copy2(bak, path)
        except (OSError, ValueError):
            logger.warning(f"[SETTINGS] {path.name} is damaged and has no good copy; it starts fresh")
            continue
        logger.warning(f"[SETTINGS] {path.name} was damaged; restored the last good copy")
        restored.append(path.name)
    return restored
