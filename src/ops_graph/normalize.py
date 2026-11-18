"""Hostname normalization.

Every source spells the same machine differently:

    CMDB        SRV-PAR-APP02
    monitoring  app-02.par.acme.local
    catalog     app02, srv-par-app02, db1-lyo, db01.par

This module turns any of those spellings into one comparable key,
(stem, number, site), e.g. ("app", 2, "par"). It knows nothing about the
graph: reconciliation and document linking both build on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SITES = frozenset({"par", "lyo"})
DOMAIN_SUFFIX = ".acme.local"
PREFIXES = frozenset({"srv"})

_TOKEN_SPLIT = re.compile(r"[.\-_\s]+")
_STEM_NUMBER = re.compile(r"^(?P<stem>.*?)(?P<number>\d+)$")


@dataclass(frozen=True)
class HostKey:
    """Canonical identity of a hostname spelling.

    `number` is an int so that "app01" and "app1" compare equal.
    `site` is None when the spelling does not say where the host lives.
    """

    stem: str
    number: int | None
    site: str | None

    @property
    def name(self) -> str:
        return self.stem if self.number is None else f"{self.stem}{self.number:02d}"

    def with_site(self, site: str | None) -> HostKey:
        """Fill in a site taken from context (e.g. the catalog entry), never overriding one."""
        if self.site is not None or site is None:
            return self
        return HostKey(self.stem, self.number, site.lower())

    def __str__(self) -> str:
        return self.name if self.site is None else f"{self.site}/{self.name}"


def parse_host(raw: str) -> HostKey:
    """Parse any hostname spelling into a HostKey.

    Grammar, applied in order:
      1. lowercase, drop the ".acme.local" domain;
      2. split on dots, hyphens, underscores and spaces;
      3. drop the "srv" prefix token;
      4. the token that names a known site (par, lyo) becomes the site,
         wherever it sits (FQDN label, hyphen suffix, CMDB infix);
      5. the remaining tokens are glued together and split into a
         letter stem and a trailing number ("app-02" -> "app", 2).

    Raises ValueError when nothing is left to identify the host, or when
    the spelling names two different sites.
    """
    text = raw.strip().lower()
    if text.endswith(DOMAIN_SUFFIX):
        text = text[: -len(DOMAIN_SUFFIX)]

    tokens = [t for t in _TOKEN_SPLIT.split(text) if t]
    if tokens and tokens[0] in PREFIXES:
        tokens = tokens[1:]

    sites = {t for t in tokens if t in SITES}
    if len(sites) > 1:
        raise ValueError(f"hostname {raw!r} names several sites: {sorted(sites)}")
    site = sites.pop() if sites else None

    rest = "".join(t for t in tokens if t not in SITES)
    if not rest:
        raise ValueError(f"hostname {raw!r} has no host part")

    match = _STEM_NUMBER.match(rest)
    if match and match.group("stem"):
        return HostKey(match.group("stem"), int(match.group("number")), site)
    return HostKey(rest, None, site)
