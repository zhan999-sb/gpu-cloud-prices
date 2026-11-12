"""Explicit hourly GPU promotional cards, display-only and always from-price.

The page does not state a whole-GPU allocation count. Prices retain package
scope and per-GPU-hour stays null until the allocation is explicitly stated.
SXM4 and RTX 4090 24G/48G variants are retained separately.
"""

import re

from .anygpu import gpu_fields
from . import Document, finish, memory, money, one, record, require

URLS = ("https://www.ai-galaxy.cn/",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    cards = doc.root.find(cls="gpu-card")
    require(bool(cards), "ai-galaxy GPU promo cards missing")
    rows = []
    for card in cards:
        require(card.end is not None, "Unclosed ai-galaxy GPU card")
        bars = card.find(cls="price-bar")
        if not bars:
            continue  # The same card class is also used by the 'view more' CTA.
        bar = one(bars, "ai-galaxy price bar")
        # Match the explicit unit inside this card, never another card/banner.
        price = re.fullmatch(r"[¥￥]\s*(\d+(?:\.\d+)?)\s*(?:元\s*/\s*小时|(?:元\s*)?/\s*h)\s*立即使用", bar.text(), re.I)
        if not price:
            continue
        titles = [n for n in card.find(tag="div")
                  if "font-[500]" in n.attrs.get("class", "").split()]
        title = one(titles, "ai-galaxy GPU title").text()
        capacity = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*G(?:B)?\b", title, re.I)
        vram = memory(capacity.group(1) + "GB") if capacity else None
        raw = title[:capacity.start()].strip() if capacity else title
        explicit = re.match(r"([1-9]\d*)\s*(?:[x×]|卡)\s*(.+)", raw)
        count = int(explicit.group(1)) if explicit else None
        if explicit:
            raw = explicit.group(2)
        amount = money(price.group(1))
        rows.append(record(
            "ai_galaxy", TIER, URLS[0], fetched_at, **gpu_fields(raw, vram),
            checked_at=fetched_at, collected_at=fetched_at,
            product="GPU宣传下限价", sku=title, gpu_count=count,
            gpu_fraction=1.0 if count else None, vram_raw=vram,
            price_per_hour=amount, price_per_gpu_hour=amount / count if count else None,
            currency="CNY", price_type="on_demand", price_scope="instance",
            billing_unit="instance-hour", is_from_price=True,
            notes=["Promotional floor price; allocation count and availability unconfirmed."],
        ))
    return finish(rows) if rows else []
