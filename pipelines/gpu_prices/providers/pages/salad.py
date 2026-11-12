"""Blocked by HTTP 403 on 2026-11-12; no verified price fixture.

Future support must include Lowest/Low/Medium/High priority in product/SKU,
mark interruptible pricing and retain per-second billing. Included CPU/RAM
does not establish a uniform CPU/RAM configuration or rentable inventory.
"""

from . import NotSupported, ParseError, unsupported

URLS = ("https://salad.com/pricing/",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    unsupported(html, "salad", "plain GET returned 403 on 2026-11-12")
