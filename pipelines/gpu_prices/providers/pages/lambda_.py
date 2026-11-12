"""Instances tabs only; exclude the committed 1-Click Clusters tables.

All returned prices are per GPU and become effective 2026-11-16 at 00:00 UTC.
The date is validated against each tab's footnote before any rows are returned.
"""

from . import Document, ParseError, finish, match, memory, money, normalize_gpu, one, record, require

URLS = ("https://lambda.com/pricing",)
TIER = "B"
EFFECTIVE_FROM = "2026-11-16T00:00:00Z"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    section = one([n for n in doc.root.find(tag="section")
                   if any(h.text() == "Instances pricing" for h in n.find(tag="h2"))],
                  "Lambda Instances section")
    tabs = section.find(role="tab")
    require(len(tabs) == 4, "Expected Lambda 8x/4x/2x/1x tabs")
    counts = set()
    rows = []
    for tab in tabs:
        count = int(match(r"(8|4|2|1)x", tab.text(), "GPU tab").group(1))
        require(count not in counts, "Duplicate Lambda GPU count tab")
        counts.add(count)
        panel = one(section.find(role="tabpanel", id=tab.attrs.get("aria-controls")), "GPU tab panel")
        table = one(panel.find(tag="table"), "Instances price table")
        headers = one(table.find(tag="thead"), "Instances headers")
        require([n.text() for n in headers.find(tag="th")] == [
            "Plan", "VRAM/GPU", "vCPUs", "RAM", "STORAGE", "PRICE/GPU/HR*"
        ], "Lambda Instances columns changed")
        footnote = one(panel.find(cls="table-footer"), "Instances effective date").text()
        require("These prices take effect November 16, 2026, at 00:00 UTC." in footnote,
                "Lambda effective date changed or missing")
        require("Plus applicable sales tax/VAT/GST" in footnote, "Lambda tax footnote changed")
        body = one(table.find(tag="tbody"), "Instances body")
        require(bool(body.find(tag="tr")), "Empty Lambda tab")
        for tr in body.find(tag="tr"):
            require(tr.end is not None, "Unclosed Lambda row")
            cells = [c for c in tr.children if getattr(c, "tag", None) in {"td", "th"}]
            require(len(cells) == 6, "Incomplete Lambda instance row")
            raw, vram, cpu, ram, storage, price = [n.text() for n in cells]
            match(r"\d+", cpu, "vCPUs")
            require(raw.startswith("NVIDIA "), "Unexpected Lambda model")
            rows.append(record(
                "lambda", TIER, URLS[0], fetched_at,
                product="Instances", sku=f"{count}x {raw} {vram}",
                gpu_model_raw=raw, gpu_model=normalize_gpu(raw, memory(vram)),
                gpu_count=count, gpu_fraction=1.0, vram_raw=vram,
                vcpu=int(cpu), ram_raw=memory(ram), price_per_hour=money(price),
                currency="USD", price_type="on_demand", price_scope="per_gpu",
                billing_unit="GPU-hour", tax_basis="excluded", effective_from=EFFECTIVE_FROM,
            ))
    return finish(rows)
