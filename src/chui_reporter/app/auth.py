"""Access control: one shared password, a signed cookie.

This is an internal tool for one fund's team, handling LP-confidential figures, so the aim is a
sturdy gate rather than user accounts: a shared password held in the server's environment, a
signed HttpOnly session cookie, throttled attempts, and a same-origin check on every write.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

SESSION_DAYS = 14
COOKIE = "chui_session"


class Auth:
    def __init__(self, password: str | None, secret: str | None = None, *, dev: bool = False):
        self.password = password or ""
        self.dev = dev
        self.disabled = dev and not password
        base = secret or (hashlib.sha256(("chui-session|" + self.password).encode()).hexdigest()
                          if self.password else secrets.token_hex(32))
        self._key = base.encode()
        self._fails: dict[str, deque] = defaultdict(deque)

    @classmethod
    def from_env(cls) -> Auth:
        dev = os.environ.get("CHUI_ENV", "").lower() == "dev"
        pw = os.environ.get("CHUI_ACCESS_PASSWORD", "")
        if not pw and not dev:
            raise RuntimeError("CHUI_ACCESS_PASSWORD is not set. Set a shared password in the server's "
                               "environment (or CHUI_ENV=dev for local use only).")
        return cls(pw, os.environ.get("CHUI_SESSION_SECRET"), dev=dev)

    # -- cookie
    def _sig(self, issued: str) -> str:
        return hmac.new(self._key, issued.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        issued = str(int(time.time()))
        return f"{issued}.{self._sig(issued)}"

    def valid(self, token: str | None) -> bool:
        if self.disabled:
            return True
        if not token or "." not in token:
            return False
        issued, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, self._sig(issued)):
            return False
        try:
            return time.time() - int(issued) < SESSION_DAYS * 86400
        except ValueError:
            return False

    # -- password with throttling
    def throttled(self, who: str, now: float | None = None) -> bool:
        now = now or time.time()
        q = self._fails[who]
        while q and now - q[0] > 60:
            q.popleft()
        return len(q) >= 5

    def check(self, who: str, attempt: str, now: float | None = None) -> bool:
        now = now or time.time()
        if self.throttled(who, now):
            return False
        ok = bool(self.password) and hmac.compare_digest(attempt.encode(), self.password.encode())
        if not ok:
            self._fails[who].append(now)
        else:
            self._fails.pop(who, None)
        return ok
