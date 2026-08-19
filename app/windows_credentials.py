from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def read_generic_credential(target_name: str) -> str | None:
    """Lee una credencial generica de Windows sin exponerla en logs."""
    if sys.platform != "win32":
        return None

    credential_pointer = ctypes.POINTER(_CredentialW)()
    advapi32 = ctypes.WinDLL("Advapi32.dll")
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CredentialW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]

    if not advapi32.CredReadW(target_name, 1, 0, ctypes.byref(credential_pointer)):
        return None
    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        for encoding in ("utf-16-le", "utf-8"):
            try:
                value = blob.decode(encoding).rstrip("\x00").strip()
            except UnicodeDecodeError:
                continue
            if value.startswith(("postgres://", "postgresql://")):
                return value
        return None
    finally:
        advapi32.CredFree(credential_pointer)


def write_generic_credential(target_name: str, value: str, username: str) -> None:
    """Guarda un secreto como credencial generica persistente de Windows."""
    if sys.platform != "win32":
        raise RuntimeError("Windows Credential Manager solo esta disponible en Windows.")
    encoded = value.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = _CredentialW()
    credential.Type = 1
    credential.TargetName = target_name
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = 2
    credential.UserName = username
    advapi32 = ctypes.WinDLL("Advapi32.dll")
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError()
