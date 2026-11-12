"""Public partner configuration; never used as a GPU price source."""

import json
from pathlib import Path
from urllib.parse import urlsplit


def surflare():
    config = Path(__file__).resolve().parents[2] / "config" / "affiliates.json"
    entry = json.loads(config.read_text(encoding="utf-8"))["surflare"]
    partner = {key: entry[key] for key in ("url", "label", "promo_code")}
    if any(not isinstance(value, str) or not value.strip() for value in partner.values()):
        raise ValueError("Partner configuration must contain nonempty text")
    parts = urlsplit(partner["url"])
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or any(c.isspace() for c in partner["url"])):
        raise ValueError("Partner URL must be a public HTTPS URL")
    return partner
