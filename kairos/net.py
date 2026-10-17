"""TLS settings shared by every outbound HTTPS client (LLM, web, Telegram, calendar).

httpx trusts only its bundled `certifi` list by default. Corporate networks often intercept HTTPS with their
own root certificate, which lives in the *operating system's* store - so on such machines every request fails
with CERTIFICATE_VERIFY_FAILED. We therefore verify against the OS store (ssl.create_default_context loads the
Windows / macOS / Linux system CAs), plus an optional extra bundle from KAIROS_CA_BUNDLE. Verification is never
turned off.
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()  # system trust store
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())  # keep the public bundle too
    except Exception:
        pass
    extra = os.environ.get("KAIROS_CA_BUNDLE")
    if extra and os.path.exists(extra):
        ctx.load_verify_locations(extra)
    return ctx
