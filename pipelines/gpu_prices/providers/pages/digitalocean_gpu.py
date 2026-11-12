"""Blocked by HTTP 403 on 2026-11-12; no verified price fixture.

Future support must retain per-second billing, the 300-second minimum,
explicit GPU count and per-GPU versus instance price scope. GPU Droplets
are a separate product from Paperspace; no monthly conversion is permitted.
"""

from . import NotSupported, ParseError, unsupported

URLS = ("https://www.digitalocean.com/pricing/gpu-droplets",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    unsupported(html, "digitalocean_gpu", "plain GET returned 403 on 2026-11-12")
