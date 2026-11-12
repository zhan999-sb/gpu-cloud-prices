"""Bronze reference prices only when that column explicitly states hours.

The fetched 2026-11-12 table labels amounts only as '元', so parse returns []
for that real fixture. An unrelated later example '¥2.0/小时' does not establish
the table's unit. A future explicitly hourly bronze column is supported.
The independent GPU-information table supplies a same-document VRAM cross-check;
contradictions retain canonical model memory separately and null the offer VRAM.
"""

import re

from .anygpu import gpu_fields
from . import Document, memory, money, record, require

URLS = ("https://www.gpushare.com/docs/getting-started/car/",)
TIER = "C"
HOURLY = r"(?:元\s*/\s*小时|元\s*/\s*h\b|/\s*小时|/\s*h\b|每小时|小时价)"
# Sanity checks, not additions to the GPU-name mappings. These capacities were
# checked against this URL's independent GPU-information table on 2026-11-12;
# retain them even if a later page drops that table but keeps the erroneous row.
KNOWN_VRAM = {"3090ti": {"24GB"}, "3080ti": {"12GB"}}


def _cells(tr):
    return [c for c in tr.children if not isinstance(c, str) and c.tag in {"td", "th"}]


def _key(raw):
    raw = re.sub(r"\b(?:NVIDIA|GeForce|Tesla)\s*", "", raw, flags=re.I)
    # The specification table calls GeForce 3090Ti by a shorter spelling.
    raw = re.sub(r"\bRTX\s*", "", raw, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    record("gpushare_docs", TIER, URLS[0], fetched_at)
    tables = doc.root.find(tag="table")
    price_tables = [t for t in tables if "青铜会员" in t.text() and "GPU型号" in t.text()]
    require(len(price_tables) == 1, "Gpushare bronze reference table missing or ambiguous")
    reference = {key: set(values) for key, values in KNOWN_VRAM.items()}
    for table in tables:
        if table is price_tables[0] or "CUDA核心数量" not in table.text():
            continue
        for tr in table.find(tag="tr"):
            cells = _cells(tr)
            if len(cells) > 2 and cells[0].tag == "td":
                raw, capacity = cells[0].text(), cells[1].text()
                if re.fullmatch(r"\d+(?:\.\d+)?GB", capacity):
                    key = _key(raw)
                    if key not in KNOWN_VRAM:
                        reference.setdefault(key, set()).add(capacity)
    table = price_tables[0]
    require(table.end is not None, "Unclosed Gpushare price table")
    trs = table.find(tag="tr")
    headers = [tr for tr in trs if any("青铜会员" in c.text() for c in _cells(tr))]
    require(len(headers) == 1, "Gpushare bronze header ambiguous")
    headings = [c.text() for c in _cells(headers[0])]
    require(headings[:3] == ["类型", "GPU型号", "显存"] and len(headings) == 6,
            "Gpushare price columns changed")
    bronze = next(i for i, text in enumerate(headings) if "青铜会员" in text)
    if not re.search(HOURLY, headings[bronze], re.I):
        return []  # No global-page or nearby-example unit inference.
    rows = []
    for tr in trs:
        cells = _cells(tr)
        if not cells or cells[0].tag != "td":
            continue
        require(tr.end is not None, "Unclosed Gpushare row")
        # The first category cell has rowspan=8; subsequent rows omit that cell.
        if len(cells) == 6:
            require("rowspan" in cells[0].attrs, "Unexpected Gpushare category cell")
            cells = cells[1:]
        require(len(cells) == 5, "Gpushare row column count changed")
        raw, reported = cells[0].text(), memory(cells[1].text())
        price_text = cells[bronze - 1].text()
        price = re.fullmatch(r"(\d+(?:\.\d+)?)\s*元(?:\s*/\s*(?:小时|h))?", price_text, re.I)
        require(price is not None, "Invalid Gpushare bronze price")
        known = reference.get(_key(raw), set())
        conflict = bool(known and reported not in known)
        canonical = next(iter(known)) if len(known) == 1 else None
        fields = gpu_fields(raw, canonical if conflict else reported)
        if conflict:
            fields["flags"].append("vram_conflict")
        notes = ["Bronze member reference price; not live inventory."]
        if conflict:
            notes.append(f"Reported VRAM {reported} contradicts GPU information table; canonical {canonical or sorted(known)}.")
        rows.append(record(
            "gpushare_docs", TIER, URLS[0], fetched_at, **fields,
            checked_at=fetched_at, collected_at=fetched_at,
            product="青铜会员参考价", sku=raw + " | " + reported,
            vram_raw=None if conflict else reported,
            canonical_vram_raw=canonical, reported_vram_raw=reported,
            price_per_hour=money(price.group(1)), currency="CNY",
            price_type="on_demand", price_scope="gpu_component", billing_unit="GPU-hour",
            price_per_gpu_hour=money(price.group(1)), is_from_price=True, notes=notes,
        ))
    return rows
