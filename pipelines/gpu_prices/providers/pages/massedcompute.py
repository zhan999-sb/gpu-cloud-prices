"""Blocked by HTTP 403 on 2026-11-12; no verified price fixture.

Future support must use whole-instance row prices and explicit GPU/CPU/RAM
allocations. Multi-GPU CPU scaling must never be invented from single-GPU
marketing examples. No login or undocumented API fallback is implemented.
"""

from . import NotSupported, ParseError, unsupported

URLS = ("https://vm.massedcompute.com/pricing",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    unsupported(html, "massedcompute", "plain GET returned 403 on 2026-11-12")
