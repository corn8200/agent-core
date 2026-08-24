"""Shared helpers for services retired with the old VPS."""

from __future__ import annotations

from typing import Any

VPS_RETIRED_AT = "2026-08-17"
VPS_RETIREMENT_REASON = "deleted VPS and retired control-plane services"


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


def raise_retired(service: str) -> None:
    raise RetiredServiceError(service)
