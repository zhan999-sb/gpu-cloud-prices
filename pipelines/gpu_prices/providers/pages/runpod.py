"""Blocked by HTTP 403 on 2026-11-12; no verified price fixture.

Future support must separate Secure/Community and Pods/Serverless/Clusters,
on-demand/spot, second/hour units and explicit MIG allocation fractions.
No undocumented GraphQL/API calls are made by this module.
"""

from . import NotSupported, ParseError, unsupported

URLS = ("https://www.runpod.io/pricing",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    unsupported(html, "runpod", "plain GET returned 403 on 2026-11-12")
