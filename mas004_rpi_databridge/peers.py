from __future__ import annotations

from dataclasses import dataclass
from typing import List
from typing import Tuple

from mas004_rpi_databridge.config import Settings


@dataclass(frozen=True)
class SenderLane:
    name: str
    url_prefixes: Tuple[str, ...] = ()
    exclude_url_prefixes: Tuple[str, ...] = ()
    use_primary_watchdog: bool = False


def normalize_peer_base_url(raw: str) -> str:
    return (raw or "").strip().rstrip("/")


def primary_peer_base_url(cfg: Settings) -> str:
    return normalize_peer_base_url(cfg.peer_base_url)


def secondary_peer_base_url(cfg: Settings) -> str:
    return normalize_peer_base_url(getattr(cfg, "peer_base_url_secondary", ""))


def url_matches_peer_base(url: str, base: str) -> bool:
    normalized_url = normalize_peer_base_url(url)
    normalized_base = normalize_peer_base_url(base)
    if not normalized_url or not normalized_base:
        return False
    return normalized_url == normalized_base or normalized_url.startswith(normalized_base + "/")


def peer_base_urls(cfg: Settings) -> List[str]:
    """
    Return configured peer base URLs (primary + optional secondary), normalized:
    - stripped
    - trailing slash removed
    - deduplicated preserving order
    """
    items = []
    for raw in [cfg.peer_base_url, getattr(cfg, "peer_base_url_secondary", "")]:
        base = normalize_peer_base_url(raw)
        if not base:
            continue
        if base not in items:
            items.append(base)
    return items


def peer_urls(cfg: Settings, path: str) -> List[str]:
    p = (path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    return [f"{base}{p}" for base in peer_base_urls(cfg)]


def sender_lanes(cfg: Settings) -> List[SenderLane]:
    primary_base = primary_peer_base_url(cfg)
    if primary_base:
        return [
            SenderLane(
                name="primary",
                url_prefixes=(primary_base,),
                use_primary_watchdog=True,
            ),
            SenderLane(
                name="aux",
                exclude_url_prefixes=(primary_base,),
                use_primary_watchdog=False,
            ),
        ]

    return [SenderLane(name="default")]
