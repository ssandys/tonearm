"""Which zone does the bar follow?

Auto-follow the most recently started zone; a pin overrides. "Most recently
started" is the TRANSITION into playing, not membership of the playing set --
otherwise an unchanged push would re-stamp every playing zone and the bar would
oscillate between rooms.
"""

from __future__ import annotations

import itertools

ACTIVE = ("playing", "loading")


class Arbiter:
    def __init__(self, pinned_id: str | None = None) -> None:
        self.pinned_id = pinned_id
        self._counter = itertools.count()
        self._started_at: dict[str, int] = {}
        self._last_state: dict[str, str] = {}
        self._last_followed: str | None = None

    def pin(self, zone_id: str) -> None:
        self.pinned_id = zone_id

    def unpin(self) -> None:
        self.pinned_id = None

    def observe(self, zones: list[dict]) -> None:
        """Record play-start transitions. Call once per Roon update, with
        the complete current zone listing, immediately before `select()`.

        `_last_followed` is maintained here rather than in `select()`, so
        that `select()` stays a pure read. It must track the current
        winner among zones active IN THIS LISTING -- not just "the last
        zone that ever transitioned into playing" -- because the winner
        can also change when some OTHER zone drops out of the active set
        (e.g. it pauses). That is not a transition into playing for
        anyone, so a version of this method that only updated
        `_last_followed` inside the transition check above would leave it
        stuck on a zone that is no longer active, and a later "nothing is
        active" cycle would incorrectly fall back to that stale zone
        instead of the one that was actually still playing.
        """
        for zone in zones:
            zid = zone.get("id", "")
            now = zone.get("state", "stopped")
            was = self._last_state.get(zid)
            if now in ACTIVE and was not in ACTIVE:
                self._started_at[zid] = next(self._counter)
            self._last_state[zid] = now

        active_ids = [z.get("id", "") for z in zones if z.get("state") in ACTIVE]
        if active_ids:
            # Same ranking as select()'s active branch: most recently
            # started wins, ties broken by id. Recomputing here (rather
            # than only on a transition) is what lets the winner update
            # when the *previous* winner drops out without anyone new
            # starting.
            self._last_followed = max(
                active_ids, key=lambda zid: (self._started_at.get(zid, -1), zid))

    def select(self, zones: list[dict]) -> dict | None:
        by_id = {z.get("id", ""): z for z in zones}

        if self.pinned_id and self.pinned_id in by_id:
            return self._mark(by_id[self.pinned_id], pinned=True)

        active = [z for z in zones if z.get("state") in ACTIVE]
        if active:
            # Ties broken by id so the choice is deterministic rather than
            # dependent on the order Roon happened to list zones in.
            best = max(active, key=lambda z: (self._started_at.get(z.get("id", ""), -1),
                                              z.get("id", "")))
            return self._mark(best, pinned=False)

        # Nothing active: keep showing the zone we were following, so pausing
        # does not blank the widget or send it to another room.
        if self._last_followed and self._last_followed in by_id:
            return self._mark(by_id[self._last_followed], pinned=False)
        return None

    @staticmethod
    def _mark(zone: dict, pinned: bool) -> dict:
        # A copy: callers keep their listing, and select() must be free of
        # side effects so it can be called more than once per update.
        out = dict(zone)
        out["pinned"] = pinned
        return out
