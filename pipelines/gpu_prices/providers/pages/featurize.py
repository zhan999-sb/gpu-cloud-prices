"""Visible instance cards only; ignore hydration host IPs and physical totals.

The visible card does not state the GPU allocation count. 'N 卡可用' is an
inventory observation, not gpu_count. GB strings are retained verbatim.
"""

import re
from . import Document, ParseError, finish, memory, money, normalize_gpu, one, record, require

URLS = ("https://featurize.cn/vm/available",)
TIER = "B"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    cards = [n for n in doc.root.find(tag="div")
             if any(c.startswith("host-machine_root__")
                    for c in n.attrs.get("class", "").split())]
    require(bool(cards), "Featurize instance cards missing")
    rows = []
    for card in cards:
        require(card.end is not None, "Unclosed Featurize card")
        title = one(card.find(tag="h3"), "instance title").text()
        if title == "CPU":
            continue
        text = card.text()
        price = re.search(r"按量使用\s*¥\s*(\d+(?:\.\d+)?)\s*/\s*小时", text)
        gpu = re.search(r"GPU\s+(.+?)\s*，\s*共\s*(\d+(?:\.\d+)?\s*(?:GB|MB|B))\s*显存", text)
        cpu = re.search(r"CPU\s+(\d+)\s*核\s+", text)
        ram = re.search(r"内存\s+(\d+(?:\.\d+)?\s*GB)", text)
        require(all((price, gpu, cpu, ram)), f"Incomplete Featurize card: {title}")
        available = re.search(r"(\d+)\s*卡可用", text)
        # Availability is optional, but conflicting explicit indicators fail.
        if available:
            count = int(available.group(1))
            require(not (count > 0 and "已售罄" in text), "Conflicting inventory labels")
            availability = "available" if count else "unavailable"
        else:
            count = None
            availability = "unavailable" if "已售罄" in text else "unknown"
        vram = memory(gpu.group(2))
        rows.append(record(
            "featurize", TIER, URLS[0], fetched_at,
            product="按量使用实例", sku=title, gpu_model_raw=gpu.group(1),
            gpu_model=normalize_gpu(gpu.group(1), vram), vram_raw=vram,
            vcpu=int(cpu.group(1)), ram_raw=memory(ram.group(1)),
            price_per_hour=money(price.group(1)), currency="CNY",
            price_type="on_demand", price_scope="instance", billing_unit="instance-hour",
            availability=availability,
            availability_observed_at=fetched_at if availability != "unknown" else None,
            available_gpu_count=count,
        ))
    return finish(rows)
