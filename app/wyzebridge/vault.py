"""Secrets at rest: Wyze tokens, the web password and API key, camera keys.

On Windows they're sealed with DPAPI for the signed-in Windows user, so a copied file is useless on another
computer or account. Elsewhere (Docker, Linux) files stay as they were. Old plain files still read, and are
sealed the next time they're saved.
"""

import os
from pathlib import Path

from wyzebridge.logging import logger

MAGIC = b"WTDPAPI1"

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _UI_FORBIDDEN = 0x1  # never show a prompt: the bridge runs without a window

    def _dpapi(fn, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in, blob_out = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), _Blob()
        if not fn(ctypes.byref(blob_in), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(blob_out)):
            raise OSError(ctypes.get_last_error(), "Windows couldn't seal or open a saved secret")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))

    def _protect(data: bytes) -> bytes:
        return _dpapi(_crypt32.CryptProtectData, data)

    def _unprotect(data: bytes) -> bytes:
        return _dpapi(_crypt32.CryptUnprotectData, data)

else:
    _protect = _unprotect = None


def seal(data: bytes) -> bytes:
    if not _protect:
        return data
    try:
        return MAGIC + _protect(data)
    except OSError as ex:
        # Saving it plain beats losing the Wyze sign-in.
        logger.warning(f"[VAULT] Saving a secret unencrypted: {ex}")
        return data


def unseal(data: bytes) -> bytes:
    if not data.startswith(MAGIC):
        return data  # saved before encryption, or not on Windows
    if not _unprotect:
        raise OSError("This secret was sealed by Windows and can only be opened there")
    return _unprotect(data[len(MAGIC) :])


def sealed(path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_bytes(path) -> bytes:
    return unseal(Path(path).read_bytes())


def read_text(path) -> str:
    return read_bytes(path).decode()


def write_bytes(path, data: bytes) -> None:
    path = Path(path)
    path.touch(mode=0o600)
    path.write_bytes(seal(data))


def write_text(path, text: str) -> None:
    write_bytes(path, text.encode())


def enabled() -> bool:
    return bool(_protect)


def seal_existing(paths) -> None:
    """Seal secrets saved before encryption (or by hand), so none wait for their next save."""
    for path in paths:
        if enabled() and Path(path).is_file() and not sealed(path):
            try:
                write_bytes(path, read_bytes(path))
            except OSError as ex:
                logger.warning(f"[VAULT] Couldn't seal {Path(path).name}: {ex}")
