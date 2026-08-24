"""Shared helpers for services retired with the old VPS."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

VPS_RETIRED_AT = "2026-08-17"
VPS_RETIREMENT_REASON = "deleted VPS and retired control-plane services"
_RETIRED_ROUTE_HOSTS = {
    "vps",
    "claude-vps",
    "cornelius-vps",
    "hetzner",
    "hetzner-vps",
    "john-vps",
    ".".join(("100", "118", "21", "64")),
    ".".join(("135", "181", "4", "118")),
    ".".join(("91", "99", "157", "139")),
    "cp.jcornelius.net",
    "app.jcornelius.net",
}


class RetiredServiceError(RuntimeError):
    """Raised before a retired route can open a network or process transport."""

    def __init__(self, service: str):
        self.service = service
        super().__init__(retired_message(service))


def retired_message(service: str) -> str:
    return (
        f"{service} is retired: {VPS_RETIREMENT_REASON} "
        f"on {VPS_RETIRED_AT}"
    )


def retired_result(service: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "retired": True,
        "service": service,
        "retired_at": VPS_RETIRED_AT,
        "error": retired_message(service),
    }
    result.update(extra)
    return result


def route_host(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        try:
            return (urlsplit(raw).hostname or "").lower()
        except ValueError:
            return ""
    authority = raw.split("/", 1)[0]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("[") and "]" in authority:
        return authority[1:authority.index("]")]
    if authority.count(":") == 1:
        return authority.split(":", 1)[0]
    return authority


def is_retired_route(value: str | None) -> bool:
    raw = str(value or "").strip().lower()
    host = route_host(raw)
    return (
        raw in _RETIRED_ROUTE_HOSTS
        or host in _RETIRED_ROUTE_HOSTS
        or raw.startswith("claude-vps:")
        or raw.startswith("vps-")
    )


def reject_retired_route(value: str | None, service: str) -> None:
    if is_retired_route(value):
        raise RetiredServiceError(service)


def raise_retired(service: str) -> None:
    raise RetiredServiceError(service)
