"""Visible NORMAL hourly package prices; member/monthly prices are not offers.

The 2026-11-12 RTX 5090 normal price is literally '¥2.72.0'. It is skipped,
without repairing the typo or substituting the member price. Counts and regions
remain unknown unless explicitly stated in the individual package.
"""

import re

from ..mappings import CURATED
from . import Document, clean, finish, memory, money, normalize_gpu, one, record, require

URLS = ("https://www.anygpu.cn/",)
TIER = "C"


def _model_key(value):
    value = re.sub(r"\b(?:NVIDIA|GeForce|Tesla)\s+", "", value, flags=re.I)
    value = re.sub(r"-(?=SXM|PCIe|NVL)", " ", value, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", value.lower())


def gpu_fields(raw: str, vram: str | None = None) -> dict:
    """Shared by these prep adapters, without importing HTTP/schema/registry.

    Reuse reviewed mappings and spelling normalization, retaining unknowns.
    A normalized spelling is not a reviewed mapping. Unknown names remain raw
    in gpu_model, with an explicit flag and a separate spelling-only candidate.
    No variant (SXM/PCIe/NVL/memory) is inferred from a less specific name.
    """
    raw = clean(raw)
    candidate = normalize_gpu(re.sub(r"\bGeForce\s+", "", raw), vram)
    models = {entry["model"] for catalog in CURATED.values()
              if isinstance(catalog, dict) for entry in catalog.values()
              if isinstance(entry, dict) and "model" in entry}
    matches = [model for model in models
               if _model_key(normalize_gpu(model)) == _model_key(candidate)]
    return dict(gpu_model_raw=raw,
                gpu_model=normalize_gpu(matches[0]) if len(matches) == 1 else raw,
                gpu_model_normalized=candidate,
                flags=[] if len(matches) == 1 else ["unmapped_gpu_model"])


def _yuan(text):
    found = re.fullmatch(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
    return money(found.group(1)) if found else None


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    cards = doc.root.find(cls="plan-card")
    require(bool(cards), "AnyGPU package cards missing")
    rows = []
    for card in cards:
        title = one(card.find(cls="plan-name"), "AnyGPU plan name").text()
        # CPU/desktop packages use the same card markup, without GPU memory.
        features = " ".join(n.text() for n in card.find(cls="plan-feature"))
        vram = re.search(r"(\d+(?:\.\d+)?\s*GB)\s*显存", features)
        if not vram:
            continue
        require(card.end is not None, "Unclosed AnyGPU package")
        hourly = one([n for n in card.find(cls="price-card") if n.has_class("hourly")], "AnyGPU hourly price")
        require(one(hourly.find(cls="price-period"), "price period").text() == "小时价", "AnyGPU billing period changed")
        normal = one(hourly.find(cls="price-original"), "normal price")
        price = _yuan(normal.text())
        if price is None:
            continue  # Strict full match: '¥2.72.0' is not silently read as 2.72.
        member_nodes = hourly.find(cls="price-member")
        member = _yuan(one(member_nodes, "member price").text()) if member_nodes else None
        ram = re.search(r"(\d+(?:\.\d+)?\s*GB)\s*内存", features)
        cpu = re.search(r"(\d+)\s*核\s*CPU", features)
        disk = re.search(r"([\d.GB+\s]+\s+SSD)\b", features)
        explicit = re.search(r"(?<!\d)([1-9]\d*)\s*(?:卡|[x×]\s*(?=NVIDIA|RTX|A\d|H\d))", title + " " + features, re.I)
        count = int(explicit.group(1)) if explicit else None
        model = re.sub(r"^\s*[1-9]\d*\s*(?:卡|[x×])\s*", "", title)
        notes = ["Normal hourly package price; CPU core label retained verbatim."]
        if member is not None:
            notes.append(f"Member hourly price: CNY {member:g}; membership required.")
        rows.append(record(
            "anygpu", TIER, URLS[0], fetched_at, **gpu_fields(model, vram.group(1)),
            checked_at=fetched_at, collected_at=fetched_at,
            product="GPU云主机", sku=title + " | " + features,
            gpu_count=count, gpu_fraction=1.0 if count else None,
            vram_raw=memory(vram.group(1)), ram_raw=memory(ram.group(1)) if ram else None,
            cpu_raw=cpu.group(0) if cpu else None,
            disk_raw=disk.group(1).strip() if disk else None,
            price_per_hour=price, price_per_gpu_hour=price / count if count else None,
            currency="CNY", price_type="on_demand", price_scope="instance",
            billing_unit="instance-hour", notes=notes,
            member_price_per_hour=member,
        ))
    return finish(rows)
