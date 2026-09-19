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

        # Forget ids that are not in this listing. Both dicts are keyed on
        # zone ids straight off the wire and nothing ever removed one, so
        # they grew for the life of the process -- and not only under abuse.
        #
        # Measured on a real Core, 2026-09-19: grouping two Sonos speakers
        # creates a zone of its OWN ("Sonos Move + 1",
        # 160141644c41c0f46b48a526b5dbed57e530) which exists only while the
        # group does and vanishes on ungroup. The members keep their ids
        # across the cycle, so it is the group zone, not the members, that
        # adds an id. Whether regrouping the same pair reproduces that id or
        # mints another is NOT measured -- if it mints, the set of ids a
        # long-running daemon has seen grows with grouping activity.
        #
        # The listing is the whole of what select() can reach. Its fallback
        # to `_last_followed` is guarded by `in by_id`, so a followed zone
        # that is still selectable is in this listing already and survives on
        # that basis; one that is absent cannot be selected however much
        # state is kept for it. Pinning it separately would only retain
        # entries for ids select() has no way to use -- which is exactly
        # what a dissolved group is.
        #
        # A zone that leaves and later returns playing is then a fresh
        # transition, which is what it is from the listener's point of view:
        # previously it kept a stale counter and could outrank a zone that
        # had genuinely just started.
        live = {zone.get("id", "") for zone in zones}
        self._started_at = {k: v for k, v in self._started_at.items() if k in live}
        self._last_state = {k: v for k, v in self._last_state.items() if k in live}

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
