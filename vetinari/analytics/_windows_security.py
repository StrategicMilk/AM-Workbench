"""Windows owner identity and protected-DACL enforcement for private analytics files."""

from __future__ import annotations

import os
from pathlib import Path

_SERVICE_SID_ENV = "VETINARI_COST_STORAGE_SERVICE_SID"
_SYSTEM_SID = "S-1-5-18"


def _secure_windows_path(path: Path, *, directory: bool) -> None:
    """Require a trusted owner, apply a protected DACL, and verify the result."""
    _verify_windows_owner_identity(path)
    _apply_windows_private_acl(path, directory=directory)
    _verify_windows_private_acl(path, directory=directory)


def _verify_windows_owner_identity(path: Path) -> str:
    """Return the owner SID after rejecting identities outside the configured trust set."""
    owner_sid = _windows_path_owner_sid(path)
    expected_sids = _expected_windows_owner_sids()
    if owner_sid not in expected_sids:
        raise PermissionError(
            f"cost ledger owner SID is not the current process or configured service identity: {path}"
        )
    return owner_sid


def _verify_windows_handle_owner_identity(descriptor: int) -> str:
    """Return an open file handle's owner SID after enforcing the trusted identity set."""
    owner_sid = _windows_handle_owner_sid(descriptor)
    if owner_sid not in _expected_windows_owner_sids():
        raise PermissionError("cost ledger open handle owner is not the current process or configured service identity")
    return owner_sid


def _expected_windows_owner_sids() -> set[str]:
    """Return the current process SID plus an optional explicitly configured service SID."""
    expected = {_current_windows_user_sid()}
    configured = os.environ.get(_SERVICE_SID_ENV)
    if configured is not None:
        expected.add(_canonical_windows_sid(configured))
    return expected


def _current_windows_user_sid() -> str:
    """Resolve the user SID from the current process token."""
    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_token_information.restype = wintypes.BOOL
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not open_process_token(get_current_process(), token_query, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        get_token_information(token, token_user, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(token, token_user, buffer, required, ctypes.byref(required)):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user_data = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents
        return _sid_pointer_to_text(token_user_data.Sid)
    finally:
        close_handle(token)


def _canonical_windows_sid(raw_sid: str) -> str:
    """Validate and canonicalize a configured Windows SID string."""
    import ctypes
    from ctypes import wintypes

    sid = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSidToSidW
    convert.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    convert.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    if not convert(raw_sid, ctypes.byref(sid)):
        error = ctypes.WinError(ctypes.get_last_error())
        raise ValueError(f"{_SERVICE_SID_ENV} must be a valid Windows SID") from error
    try:
        return _sid_pointer_to_text(sid)
    finally:
        local_free(sid)


def _windows_path_owner_sid(path: Path) -> str:
    """Return a path's owner SID without trusting its discretionary ACL."""
    import ctypes
    from ctypes import wintypes

    owner = ctypes.c_void_p()
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
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    result = get_security(str(path), 1, 0x00000001, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    if result != 0:
        raise ctypes.WinError(result)
    try:
        if not owner.value or not descriptor.value:
            raise PermissionError(f"cost ledger path has no Windows owner SID: {path}")
        return _sid_pointer_to_text(owner)
    finally:
        if descriptor.value:
            local_free(descriptor)


def _windows_handle_owner_sid(descriptor: int) -> str:
    """Return the owner SID attached to an already-open file descriptor."""
    import ctypes

    msvcrt = __import__("msvcrt")

    owner = ctypes.c_void_p()
    security_descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = ctypes.c_uint32
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    operating_system_handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))
    result = get_security(
        operating_system_handle,
        1,
        0x00000001,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0:
        raise ctypes.WinError(result)
    try:
        if not owner.value or not security_descriptor.value:
            raise PermissionError("cost ledger open handle has no Windows owner SID")
        return _sid_pointer_to_text(owner)
    finally:
        if security_descriptor.value:
            local_free(security_descriptor)


def _sid_pointer_to_text(sid: object) -> str:
    """Convert a live SID pointer to canonical text."""
    import ctypes
    from ctypes import wintypes

    sid_text = wintypes.LPWSTR()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    if not convert_sid(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if sid_text.value is None:
            raise PermissionError("Windows SID conversion returned no value")
        return sid_text.value
    finally:
        if sid_text:
            local_free(ctypes.cast(sid_text, ctypes.c_void_p))


def _apply_windows_private_acl(path: Path, *, directory: bool) -> None:
    """Replace inherited access with a protected owner-and-SYSTEM DACL."""
    import ctypes
    from ctypes import wintypes

    owner_sid = _verify_windows_owner_identity(path)
    acl_descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert_descriptor.restype = wintypes.BOOL
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
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    trustee_sids = list(dict.fromkeys((owner_sid, _SYSTEM_SID)))
    inheritance = "OICI" if directory else ""
    sddl = "D:P" + "".join(f"(A;{inheritance};FA;;;{sid})" for sid in trustee_sids)
    if not convert_descriptor(sddl, 1, ctypes.byref(acl_descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not get_dacl(acl_descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl.value:
            raise PermissionError(f"cost ledger private DACL is unavailable: {path}")
        result = set_security(str(path), 1, 0x00000004 | 0x80000000, None, None, dacl, None)
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        if acl_descriptor.value:
            local_free(acl_descriptor)


def _verify_windows_private_acl(path: Path, *, directory: bool) -> None:
    """Require exactly explicit trusted-owner-and-SYSTEM full-control ACEs."""
    import ctypes
    from ctypes import wintypes

    class _Acl(ctypes.Structure):
        _fields_ = [
            ("AclRevision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]

    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = [("Header", _AceHeader), ("Mask", wintypes.DWORD), ("SidStart", wintypes.DWORD)]

    owner_sid = _verify_windows_owner_identity(path)
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    get_control.restype = wintypes.BOOL
    get_ace = advapi32.GetAce
    get_ace.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = wintypes.BOOL
    result = get_security(str(path), 1, 0x00000004, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result != 0:
        raise ctypes.WinError(result)
    try:
        if not dacl.value or not descriptor.value:
            raise PermissionError(f"cost ledger path has no private DACL: {path}")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not control.value & 0x1000:
            raise PermissionError(f"cost ledger DACL inheritance is not disabled: {path}")
        acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
        expected_sids = {owner_sid, _SYSTEM_SID}
        if acl.AceCount != len(expected_sids):
            raise PermissionError(f"cost ledger DACL is not limited to owner and SYSTEM: {path}")
        observed_sids: set[str] = set()
        expected_flags = 0x03 if directory else 0
        for index in range(acl.AceCount):
            raw_ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(raw_ace)) or not raw_ace.value:
                raise ctypes.WinError(ctypes.get_last_error())
            ace = ctypes.cast(raw_ace, ctypes.POINTER(_AccessAllowedAce)).contents
            if ace.Header.AceType != 0 or ace.Header.AceFlags & 0x10:
                raise PermissionError(f"cost ledger DACL contains a non-explicit allow ACE: {path}")
            if ace.Header.AceFlags != expected_flags:
                raise PermissionError(f"cost ledger DACL has invalid inheritance flags: {path}")
            if ace.Mask & 0x001F01FF != 0x001F01FF:
                raise PermissionError(f"cost ledger DACL does not grant full control: {path}")
            sid_address = raw_ace.value + _AccessAllowedAce.SidStart.offset
            observed_sids.add(_sid_pointer_to_text(ctypes.c_void_p(sid_address)))
        if observed_sids != expected_sids:
            raise PermissionError(f"cost ledger DACL does not contain exactly owner and SYSTEM: {path}")
    finally:
        if descriptor.value:
            local_free(descriptor)
