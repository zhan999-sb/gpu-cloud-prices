"""GPU-hour component prices; no inference of rentable GPU count.

L40S preemptible rates are explicitly excluded from dynamic spot pricing.
Ranges in CPU/RAM are preserved as provenance, never reduced to their minima.
"""

import re
from . import Document, ParseError, finish, match, money, normalize_gpu, one, record, require

URLS = ("https://nebius.com/prices",)
TIER = "B"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    blocks = [n for n in doc.root.find(cls="pc-highlight-table-block__root")
              if any(h.text() == "NVIDIA GPU Instances" for h in n.find(tag="h2"))]
    block = one(blocks, "Nebius GPU price block")
    table_rows = block.find(cls="pc-highlight-table-block__row")
    require(len(table_rows) > 1, "Nebius GPU rows missing")
    cells = lambda n: n.find(cls="pc-highlight-table-block__cell")
    require([n.text() for n in cells(table_rows[0])] == [
        "Item", "vCPUs", "RAM, GB", "Preemptible, GPU-hour *", "On-demand, GPU-hour"
    ], "Nebius GPU headers changed")
    page_text = doc.root.text()
    tax = "excluded" if "All prices are shown without any applicable taxes" in page_text else "unknown"
    flat_l40s = "NVIDIA L40S preemptible instances are excluded from spot pricing" in page_text
    rows = []
    for tr in table_rows[1:]:
        require(tr.end is not None, "Unclosed Nebius row")
        values = [n.text() for n in cells(tr)]
        require(len(values) == 5, "Nebius GPU column count changed")
        raw, cpu, ram, preemptible, demand = values
        require(raw.startswith("NVIDIA "), "Unexpected Nebius GPU model")
        match(r"\d+(?:-\d+)?", cpu, "vCPUs")
        match(r"\d+(?:-\d+)?", ram, "RAM, GB")
        for price_type, price in (("preemptible", preemptible), ("on_demand", demand)):
            if price in {"-", "Contact us"}:
                continue  # Explicit nonnumeric quotes, not missing/invalid prices.
            kind = price_type
            if kind == "preemptible":
                require("Spot prices are dynamic" in page_text, "Nebius spot footnote missing")
                if "L40S" in raw:
                    require(flat_l40s, "Nebius L40S preemptible footnote missing")
                    kind = "interruptible"
                else:
                    kind = "spot"
            rows.append(record(
                "nebius", TIER, URLS[0], fetched_at,
                product="NVIDIA GPU Instances", sku=raw, gpu_model_raw=raw,
                gpu_model=normalize_gpu(re.sub(r" with (?:Intel|AMD) CPU$", "", raw)),
                vcpu=int(cpu) if cpu.isdigit() else None, vcpu_raw=cpu,
                ram_raw=ram + " GB", price_per_hour=money(price), currency="USD",
                price_type=kind, price_scope="gpu_component", billing_unit="GPU-hour",
                is_from_price=price.lower().startswith("from "), tax_basis=tax,
            ))
    return finish(rows)
