"""Blocked by HTTP 403 on 2026-11-12; no verified price fixture.

The spec notes a 4-vCPU/32GB base and separately billed extras ($0.04/vCPU-h).
Future parsing must verify those against HTML, retain included resources and
extra charges, and distinguish minute-billed instances from second-billed
sandboxes. Spec examples are not fabricated into current observed prices.
"""

from . import NotSupported, ParseError, unsupported

URLS = ("https://www.thundercompute.com/pricing",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    unsupported(html, "thunder", "plain GET returned 403 on 2026-11-12")
