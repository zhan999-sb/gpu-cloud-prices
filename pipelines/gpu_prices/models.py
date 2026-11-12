"""Conservative GPU naming and display taxonomy, separate from price eligibility.

Read views canonicalize existing snapshots without rewriting source records.
Explicit interfaces, laptop/desktop, D/CC variants and larger modded capacities
remain distinct. Capacity corrections affect model identity, never raw fields.
"""

import re

# Usable memory commonly loses up to 6.25% (45/48, 15/16). Integer-truncated
# 7 GB reports on the explicitly identified 8 GB 4060 SKUs are also reviewed.
CAPACITIES = {
    "RTX 4090": (24,), "RTX 4090 Laptop GPU": (16,),
    "RTX 4060": (8,), "RTX 4060 Ti": (8, 16),
    "RTX 4000 Ada": (20,), "RTX 6000 Ada": (48,),
    "L40S": (48,), "P40": (24,), "P104-100": (4, 8),
    "P106-100": (6,), "V100": (16, 32),
    "V100 PCIe": (16, 32), "V100 SXM2": (16, 32),
    "RTX 3060": (8, 12), "RTX 3060 Ti": (8,),
    "RTX 3070": (8,), "RTX 3070 Ti": (8,),
    "RTX 3080": (10, 12), "RTX 3080 Ti": (12,),
    "RTX 3090": (24,), "RTX 3090 Ti": (24,),
    "RTX 4070": (12,), "RTX 4070 SUPER": (12,),
    "RTX 4070 Ti": (12,), "RTX 4070 Ti SUPER": (16,),
    "RTX 4080": (16,), "RTX 4080 SUPER": (16,),
    "RTX 5060": (8,), "RTX 5060 Ti": (8, 16),
    "RTX 5070": (12,), "RTX 5070 Ti": (16,),
    "RTX 5080": (16,), "RTX 5090": (32,),
    "RTX 5090 D": (32,), "RTX 5090 D v2": (24,),
    "RTX A4000": (16,), "RTX A5000": (24,), "RTX A6000": (48,),
    "H100 NVL": (94,), "H100 SXM": (80,), "H100 PCIe": (80,),
    "H200": (141,), "H200 SXM5": (141,), "A100": (40, 80),
    "A100 PCIe": (40, 80), "A100 SXM": (40, 80), "A100 SXM4": (40, 80),
    "A10": (24,), "A30": (24,), "L4": (24,), "T4": (16,),
}
POPULAR = ("H100", "H200", "B200", "B300", "GB300", "A100", "L40S",
           "RTX 5090", "RTX 4090", "RTX 6000 Ada", "RTX PRO 6000",
           "RTX A6000", "MI300X", "A10", "L4")
CATEGORIES = ("data_centre", "workstation", "consumer", "legacy", "other")


def canonical_model(model: str, vram_raw: str | None = None) -> str:
    original = model
    name = re.sub(r"\b(?:NVIDIA|GeForce|Tesla)\s+", "", model, flags=re.I).strip()
    name = re.sub(r"\bGeneration\b", "", name, flags=re.I)
    name = re.sub(r"\bP(104|106)-?100\b", r"P\1-100", name, flags=re.I)
    name = re.sub(r"V100\s*[- ]?\s*PCIE(?=\s|\d|$)", "V100 PCIe ", name, flags=re.I)
    name = re.sub(r"V100\s*[- ]?\s*SXM2(?=\s|\d|$)", "V100 SXM2 ", name, flags=re.I)
    memory = re.search(r"(\d+(?:\.\d+)?)\s*(?:GiB|GB)\b", name, re.I)
    capacity = float(memory.group(1)) if memory else None
    if memory:
        name = name[:memory.start()] + name[memory.end():]
    name = " ".join(name.split()).strip()
    if re.fullmatch(r'A(?:4000|5000|6000)',name,re.I):
        name = 'RTX '+name.upper()
    # Only reviewed names are assigned a spec capacity. Unknown spellings keep
    # the existing provider's model; no SKU is invented from a numeric suffix.
    key = next((key for key in CAPACITIES if key.casefold() == name.casefold()), None)
    if key is None:
        return original
    if capacity is None and vram_raw:
        reported = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:GiB|GB)\s*", vram_raw, re.I)
        capacity = float(reported.group(1)) if reported else None
    if capacity is not None:
        for spec in CAPACITIES[key]:
            if spec * .9375 <= capacity <= spec or (key in {"RTX 4060", "RTX 4060 Ti"} and capacity == 7 and spec == 8):
                capacity = spec
                break
        return f"{key} {capacity:g}GB"
    return key


def model_family(model: str) -> str:
    """Match exact family boundaries: A10 cannot swallow A100, nor 5090 D."""
    for family in POPULAR:
        if re.match(re.escape(family) + r"(?=\s|$)", model, re.I):
            return family
    return model


def category(model: str) -> str:
    if re.match(r"(?:RTX\s+(?:A\d|PRO\s|[456]000)|A[2456]\d{3}\b|Quadro|Radeon Pro|[MP][456]000)", model, re.I):
        return "workstation"
    if re.match(r"(?:GTX|GT\s|CMP|P10[46]-?100|(?:Tesla\s+)?[MP]\d|K\d)", model, re.I):
        return "legacy"
    if re.match(r"RTX\s+[2345]\d{3}", model, re.I):
        return "consumer"
    if re.match(r"(?:HGX\s+)?(?:H\d|GH\d|GB\d|B\d|A\d|L\d|T4\b|V100\b|MI\d)", model, re.I):
        return "data_centre"
    return "other"


def popular_rank(model: str) -> int:
    # Explicit variants remain cards in their own right, but desktop demand
    # must not elevate a Laptop or D variant into the popular desktop group.
    if re.search(r"Laptop|\bD\b", model, re.I):
        return len(POPULAR)
    family = model_family(model)
    return POPULAR.index(family) if family in POPULAR else len(POPULAR)


def headline_order(row):
    model = row['gpu_model']
    rank = popular_rank(model)
    memory = re.search(r"(\d+(?:\.\d+)?)GB\b", model, re.I)
    small = category(model) == 'consumer' and (memory is None or float(memory.group(1)) < 12)
    return (0, rank, row['price_per_gpu_hour'], model) if rank < len(POPULAR) else (
        1, CATEGORIES.index(category(model)), small, row['price_per_gpu_hour'], model)
