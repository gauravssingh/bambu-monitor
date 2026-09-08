"""Security and sanitization helpers for camera RTSP URLs."""

from __future__ import annotations

import re

# Matches rtsp://[user]:[pass]@host... or rtsps://
# Supports usernames containing '@' (such as email addresses) and empty usernames
RTSP_CREDENTIAL_PATTERN = re.compile(
    r"((?:rtsp|rtsps)://)(?:([^/\s]*?):)?([^:/@\s]+)@([a-zA-Z0-9_.-]+(?::\d+)?(?:/|\s|$|[?#]))",
    re.IGNORECASE,
)


def sanitize_rtsp_url(text: str) -> str:
    """Sanitize RTSP URLs in arbitrary strings by masking passwords.

    Examples:
        rtsp://admin:secret123@192.168.1.100:554/stream1 -> rtsp://admin:***@192.168.1.100:554/stream1
        rtsp://user@domain.com:pass@192.168.1.100:554/stream1 -> rtsp://user@domain.com:***@192.168.1.100:554/stream1
        rtsp://:secret123@192.168.1.100:554/stream1      -> rtsp://:***@192.168.1.100:554/stream1
        rtsp://192.168.1.100:554/stream1                 -> rtsp://192.168.1.100:554/stream1
    """
    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        proto = match.group(1)
        user = match.group(2)
        host_tail = match.group(4)
        if user:
            return f"{proto}{user}:***@{host_tail}"
        return f"{proto}:***@{host_tail}"

    return RTSP_CREDENTIAL_PATTERN.sub(_replace, text)
