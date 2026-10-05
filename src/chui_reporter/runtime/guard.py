"""Spotting an agent that has stopped making progress.

A model can get stuck repeating the same call and receiving the same answer. The step limit
eventually ends that, but only after a lot of wasted work. The guard notices the repetition
early and says so, in the agent's own conversation, so it changes approach or asks.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque


def _sig(name: str, args: dict) -> str:
    return hashlib.sha1(f"{name}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()[:12]


class LoopGuard:
    def __init__(self, window: int = 10, repeats: int = 3):
        self.recent: deque[str] = deque(maxlen=window)
        self.repeats = repeats
        self.triggered = 0

    def observe(self, name: str, args: dict) -> str | None:
        """Returns a steering message when this call has now been made `repeats` times recently."""
        sig = _sig(name, args or {})
        self.recent.append(sig)
        if sum(1 for s in self.recent if s == sig) >= self.repeats:
            self.recent.clear()
            self.triggered += 1
            return (f"You have now called {name} with the same arguments {self.repeats} times and will "
                    "get the same result again. Do not repeat it. Use what you already have, try a "
                    "different approach, or ask the user (ask_user) if you are stuck.")
        return None
