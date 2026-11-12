"""Reviewed exact SKU data, with provenance. New SKU names fail closed."""
import json
from pathlib import Path

CURATED = json.loads(Path(__file__).with_name("curated_mappings.json").read_text(encoding="utf-8"))
LINODE_SKUS = {key: (v["model"], v["count"]) for key, v in CURATED["linode"].items()}
VERDA_SKUS = {key: (v["model"], v["count"], v["raw"]) for key, v in CURATED["verda"].items()}
SCALEWAY_SKUS = {key: (v["model"], v["count"], v["raw"]) for key, v in CURATED["scaleway"].items()}
