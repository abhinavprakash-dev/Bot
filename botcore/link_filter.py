from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LinkFilterConfig:
    whitelisted_role_ids: frozenset[int] = frozenset()
    whitelisted_channel_ids: frozenset[int] = frozenset()
    whitelisted_domains: frozenset[str] = frozenset()
    allowed_tlds: frozenset[str] = frozenset({"com", "net", "org", "io", "gg", "co", "me", "dev", "app", "fm"})


URL_RE = re.compile(r"(?i)(?:https?://|www\.)[^\s<>()]+")
DOMAIN_RE = re.compile(r"(?i)(?<![@\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>()]*)?")


def _domain(value: str) -> str:
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().rstrip(".")


def contains_link(content: str, config: LinkFilterConfig) -> tuple[bool, list[str]]:
    candidates = URL_RE.findall(content) + DOMAIN_RE.findall(content)
    links: list[str] = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,!?;:)]}")
        domain = _domain(candidate)
        if not domain or domain in config.whitelisted_domains:
            continue
        if "://" not in candidate and not candidate.lower().startswith("www."):
            tld = domain.rsplit(".", 1)[-1]
            if tld not in config.allowed_tlds:
                continue
        if candidate not in links:
            links.append(candidate)
    return bool(links), links
