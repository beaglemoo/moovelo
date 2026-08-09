"""Opaque handles for places the assistant may route through.

This is the mechanism that keeps the model out of the coordinate business.
Tools return handles - `place:3`, `loop:1` - and the tools that build a
route accept only handles. A latitude and longitude the model invents is
rejected twice over: the argument schema will not accept the shape, and
the table will not resolve a string it did not mint.

The table lives for one request. Handles minted in an earlier turn survive
because the client echoes them back, which keeps the server free of any
conversation store.
"""

from dataclasses import dataclass
from typing import Literal

from app.schemas import Waypoint

HandleKind = Literal["place", "poi", "current", "centre", "loop"]

# Ceiling on echoed handles, so a long conversation cannot grow the request
# without bound.
MAX_KNOWN_HANDLES = 200


class UnknownHandleError(Exception):
    """The model used a handle the table never issued.

    Recoverable: it is reported back as a tool error so the model can search
    again, exactly like any other bad argument.
    """


@dataclass(frozen=True)
class Handle:
    ref: str
    kind: HandleKind
    name: str
    lat: float
    lon: float
    # Set only for handles that stand for a whole route rather than a point,
    # so picking a loop candidate expands to its full waypoint sequence here
    # rather than asking the model to repeat coordinates back.
    waypoints: tuple[Waypoint, ...] | None = None


class HandleTable:
    def __init__(self) -> None:
        self._handles: dict[str, Handle] = {}
        self._counters: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._handles)

    def mint(
        self,
        kind: HandleKind,
        name: str,
        lat: float,
        lon: float,
        waypoints: list[Waypoint] | None = None,
    ) -> Handle:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        handle = Handle(
            ref=f"{kind}:{self._counters[kind]}",
            kind=kind,
            name=name,
            lat=lat,
            lon=lon,
            waypoints=tuple(waypoints) if waypoints is not None else None,
        )
        self._handles[handle.ref] = handle
        return handle

    def adopt(self, handle: Handle) -> None:
        """Take a handle minted in an earlier turn, echoed back by the client.

        Counters advance past it so a later mint in this turn cannot collide
        with, and silently replace, a handle the model still refers to.
        """
        self._handles[handle.ref] = handle
        _, _, suffix = handle.ref.partition(":")
        if suffix.isdigit():
            self._counters[handle.kind] = max(self._counters.get(handle.kind, 0), int(suffix))

    def seed_context(self, waypoints: list[Waypoint], centre: Waypoint | None) -> None:
        """Expose what is already on the user's screen.

        These coordinates come from the client, never from the model, and the
        model only ever sees the label.
        """
        for index, wp in enumerate(waypoints):
            handle = Handle(
                ref=f"current:{index}",
                kind="current",
                name=_positional_name(index, len(waypoints)),
                lat=wp.lat,
                lon=wp.lon,
            )
            self._handles[handle.ref] = handle
        if centre is not None:
            self._handles["centre:map"] = Handle(
                ref="centre:map",
                kind="centre",
                name="the centre of the map",
                lat=centre.lat,
                lon=centre.lon,
            )

    def get(self, ref: str) -> Handle:
        handle = self._handles.get(ref)
        if handle is None:
            raise UnknownHandleError(
                f"No such reference: {ref}. Use a reference returned by a previous "
                f"tool call, or search again to obtain one."
            )
        return handle

    def point(self, ref: str) -> Waypoint:
        handle = self.get(ref)
        return Waypoint(lat=handle.lat, lon=handle.lon)

    def waypoints(self, ref: str) -> list[Waypoint]:
        """Expand a handle into the waypoints it stands for.

        A loop handle expands to its whole sequence; anything else is a single
        point. This is why picking a loop candidate never requires the model
        to restate a route.
        """
        handle = self.get(ref)
        if handle.waypoints is not None:
            return list(handle.waypoints)
        return [Waypoint(lat=handle.lat, lon=handle.lon)]

    def export(self) -> list[Handle]:
        """Handles worth echoing back next turn.

        `current:` and `centre:` are rebuilt from the request each time, so
        only the looked-up ones need carrying.
        """
        return [h for h in self._handles.values() if h.kind in ("place", "poi", "loop")][
            -MAX_KNOWN_HANDLES:
        ]


def _positional_name(index: int, total: int) -> str:
    if index == 0:
        return "the current start"
    if index == total - 1:
        return "the current finish"
    return f"current via point {index}"
