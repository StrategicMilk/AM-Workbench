"""Private runtime-file creation and platform permission hardening."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def secure_private_path(path: Path, *, directory: bool) -> None:
    """Enforce owner-only POSIX mode or protected owner/SYSTEM Windows ACL.

    Raises:
        OSError: If the platform permission boundary cannot be enforced.
    """
    if os.name == "nt":
        _secure_windows_path(path, directory=directory)
        return
    expected_mode = 0o700 if directory else 0o600
    path.chmod(expected_mode)
    observed = path.lstat()
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(observed.st_mode) or stat.S_IMODE(observed.st_mode) != expected_mode:
        raise PermissionError(f"private runtime path has unsafe permissions: {path}")


def _secure_windows_path(path: Path, *, directory: bool) -> None:
    import ctypes
    from ctypes import wintypes

    owner = ctypes.c_void_p()
    owner_descriptor = ctypes.c_void_p()
    owner_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    set_security = advapi32.SetNamedSecurityInfoW
    set_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security.restype = wintypes.DWORD
    result = get_security(
        str(path),
        1,
        0x00000001,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(owner_descriptor),
    )
    if result != 0:
        raise ctypes.WinError(result)
    try:
        if not owner.value or not convert_sid(owner, ctypes.byref(owner_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        owner_sid = owner_text.value
    finally:
        if owner_text:
            kernel32.LocalFree(owner_text)
        if owner_descriptor.value:
            kernel32.LocalFree(owner_descriptor)
    sddl = f"D:P(A;OICI;FA;;;{owner_sid})(A;OICI;FA;;;SY)" if directory else f"D:P(A;;FA;;;{owner_sid})(A;;FA;;;SY)"
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise PermissionError(f"private runtime ACL is unavailable: {path}")
        result = set_security(str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None)
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        kernel32.LocalFree(descriptor)
