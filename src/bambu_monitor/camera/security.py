"""Security and sanitization helpers for camera RTSP URLs."""

from __future__ import annotations

import re

# Fallback pattern for RTSP URLs embedded inside larger text (e.g. ffmpeg
# stderr). Only used to locate the credential portion; masking itself is
# done structurally where possible.
RTSP_URL_IN_TEXT_PATTERN = re.compile(
    r"(?:rtsps?://)\S+",
    re.IGNORECASE,
)

_SCHEME_PREFIXES = ("rtsp://", "rtsps://")


def _mask_url(url: str) -> str:
    """Mask the password in a single RTSP URL, if present.

    Deliberately avoids urllib.parse.urlsplit: it treats the first
    unescaped '/' after '://' as the start of the path, which truncates
    the userinfo (and therefore the password) whenever a password
    contains a literal '/'. Real camera-generated RTSP URLs routinely
    embed raw, unescaped passwords, so that boundary can't be trusted.
    Instead, the host is never allowed to contain '@', so the LAST '@'
    before the path always marks the true userinfo/host boundary,
    regardless of '@', ':', or '/' inside the password.
    """
    lower = url.lower()
    scheme_len = next((len(p) for p in _SCHEME_PREFIXES if lower.startswith(p)), None)
    if scheme_len is None:
        return url

    rest = url[scheme_len:]
    at_index = rest.rfind("@")
    if at_index == -1:
        return url

    credentials, host_and_path = rest[:at_index], rest[at_index + 1 :]
    username, sep, password = credentials.partition(":")
    if not sep or not password:
        return url

    prefix = url[:scheme_len]
    return f"{prefix}{username}:***@{host_and_path}"


def sanitize_rtsp_url(text: str) -> str:
    """Sanitize RTSP URLs in arbitrary strings by masking passwords.

    Uses structural URL parsing so passwords containing '@', ':', or '/'
    are masked reliably (a regex over the password charset would leak them).

    Examples:
        rtsp://admin:secret123@192.168.1.100:554/stream1 -> rtsp://admin:***@192.168.1.100:554/stream1
        rtsp://admin:pa/ss@192.168.1.100:554/stream1     -> rtsp://admin:***@192.168.1.100:554/stream1
        rtsp://user@domain.com:p@ss/w0rd@192.168.1.100:554/stream1 -> rtsp://user@domain.com:***@192.168.1.100:554/stream1
        rtsp://:secret123@192.168.1.100:554/stream1      -> rtsp://:***@192.168.1.100:554/stream1
        rtsp://192.168.1.100:554/stream1                 -> rtsp://192.168.1.100:554/stream1
    """
    if not text:
        return text
    if "://" not in text:
        return text

    # Fast path: text is (mostly) a single URL.
    if text.lower().startswith(("rtsp://", "rtsps://")) and "\n" not in text:
        return _mask_url(text)

    # General case: mask each RTSP URL found inside a larger string
    # (e.g. ffmpeg/ffprobe stderr dumps).
    return RTSP_URL_IN_TEXT_PATTERN.sub(lambda m: _mask_url(m.group(0)), text)
