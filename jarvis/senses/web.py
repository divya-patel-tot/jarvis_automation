"""Fetch web page text from allowlisted hosts (no cookies, no user profile).

Output is UNTRUSTED. Only http(s), only hosts in the allowlist, size-capped.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

from jarvis.senses.documents import html_to_text

MAX_BYTES = 3_000_000


def host_allowed(url: str, allowlist: list[str]) -> bool:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in allowlist):
        return False
    try:  # no internal network targets (SSRF)
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
    except socket.gaierror:
        return False
    return True


def fetch_text(url: str, allowlist: list[str], timeout: float = 15.0) -> tuple[str, str]:
    """Return (title, text). Raises ValueError if not allowed."""
    if not host_allowed(url, allowlist):
        raise ValueError(f"host not in web allowlist: {urlparse(url).hostname}")
    import httpx

    with httpx.Client(follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) JarvisReader/1.0"}) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            if not host_allowed(str(r.url), allowlist):
                raise ValueError("redirected outside the allowlist")
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    break
            ctype = r.headers.get("content-type", "")
    html = data.decode("utf-8", errors="replace")
    if "html" not in ctype and not html.lstrip().startswith("<"):
        return url, html
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else url
    return title, html_to_text(html)
