from __future__ import annotations

from typing import List

from mas004_rpi_databridge.config import Settings


def peer_base_urls(cfg: Settings) -> List[str]:
    """
    Return configured peer base URLs (primary + optional secondary), normalized:
    - stripped
    - trailing slash removed
    - deduplicated preserving order
    """
    items = []
    for raw in [cfg.peer_base_url, getattr(cfg, "peer_base_url_secondary", "")]:
        base = (raw or "").strip().rstrip("/")
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
