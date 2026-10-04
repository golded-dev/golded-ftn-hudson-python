"""Classic FTN address kludges, following the MSG reader contract."""

import re
from collections.abc import Sequence
from dataclasses import replace

from golded_ftn import ControlLine, FtnAddress, MessageControlLines, parse_message


def uint16(value: int) -> int:
    if not 0 <= value <= 65535:
        raise ValueError(f"Value outside unsigned 16-bit range: {value}")
    return value


def resolve_addresses(
    words: dict[str, int], controls: Sequence[ControlLine]
) -> tuple[FtnAddress | None, FtnAddress | None]:
    parts: dict[str, list[int | None]] = {}
    for side in ("from", "to"):
        zone, net, node, point = (
            words[f"{side}_{key}"] for key in ("zone", "net", "node", "point")
        )
        # A nonzero net establishes the header node, including coordinator node 0.
        # Zero zone/net/point words remain absent for legacy kludge supplementation.
        parts[side] = [
            zone or None,
            net or None,
            node if node or net else None,
            point or None,
        ]

    declared: dict[tuple[str, int], int] = {}

    def merge(side: str, values: Sequence[int], *, point_only: bool = False) -> None:
        for index, value in enumerate(values):
            if point_only and index != 3:
                continue
            key = (side, index)
            if key in declared and declared[key] != value:
                raise ValueError(f"Conflicting {side} address kludges")
            declared[key] = value
            uint16(value)
            old = parts[side][index]
            if old is not None and old != value:
                raise ValueError(f"Conflicting {side} address metadata")
            parts[side][index] = value

    for control in controls:
        if control.name.upper() == "INTL":
            tokens = control.value.split()
            if len(tokens) != 2:
                continue
            addresses = [FtnAddress.try_from_string(token) for token in tokens]
            if any(
                address is None
                or address.domain is not None
                or address.point is not None
                for address in addresses
            ):
                continue
            for side, address in zip(("to", "from"), addresses, strict=True):
                assert address is not None
                merge(side, (address.zone, address.net, address.node))
        elif control.name.upper() in ("FMPT", "TOPT") and re.fullmatch(
            r"[0-9]+", control.value
        ):
            side = "from" if control.name.upper() == "FMPT" else "to"
            merge(side, (0, 0, 0, int(control.value)), point_only=True)
    result: list[FtnAddress | None] = []
    for side in ("from", "to"):
        resolved_zone, resolved_net, resolved_node, resolved_point = parts[side]
        result.append(
            FtnAddress(
                zone=resolved_zone,
                net=resolved_net,
                node=resolved_node,
                point=resolved_point or None,
            )
            if resolved_zone is not None
            and resolved_net is not None
            and resolved_node is not None
            else None
        )
    return result[0], result[1]


def parse_controls(text: str) -> MessageControlLines:
    """Include traditional space-separated address kludges beside core syntax."""
    parsed = parse_message(text)
    controls: list[ControlLine] = []
    existing = iter(parsed.kludges)
    path: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", text):
        line = raw.rstrip("\x00")
        route = re.fullmatch(r"(?:\x01)?PATH:\s*(.*)", line)
        if route:
            path.append(route[1].strip())
        if re.fullmatch(r"\x01([A-Za-z][A-Za-z0-9-]*):\s*(.*)", line):
            controls.append(next(existing))
        else:
            match = re.fullmatch(r"\x01(INTL|FMPT|TOPT)\s+(.+)", line, re.IGNORECASE)
            if match:
                controls.append(
                    ControlLine(name=match[1].upper(), value=match[2].strip(), raw=raw)
                )
    return replace(parsed, kludges=tuple(controls), path=tuple(path))
