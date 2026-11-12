"""Hourly GPU compute cards; exclude hidden stale cards, VDI and monthly plans.

Starred offers are conditional, three-year reserved rates and from-prices.
The multi-GPU variants advertised nearby are not assumed to be single-GPU
counts and are not expanded into unobserved instance rows.
"""

import re
from . import Document, ParseError, finish, match, memory, money, normalize_gpu, one, record, require

URLS = ("https://www.paperspace.com/pricing",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    hourly = one([n for n in doc.root.walk() if n.attrs.get("data-w-tab") == "Hourly"
                  and n.has_class("w-tab-pane")], "Paperspace Hourly tab")
    cards = [n for n in hourly.find(cls="instance-type-holder-flex")
             if n.visible() and n.find(cls="instance-names")]
    require(bool(cards), "Paperspace GPU compute cards missing")
    rows = []
    for card in cards:
        title = one(card.find(cls="instance-names"), "GPU card title").text()
        price_box = one([n for n in card.find(cls="instance-price-wrap") if n.visible()],
                        "visible hourly price")
        price = one(price_box.find(cls="instance-price-2"), "numeric GPU price").text()
        require("/ hour" in price_box.text() and "$" in price_box.text(), "GPU hourly unit changed")
        specs = one([n for n in card.find(cls="instance-specs-list-2") if n.visible()],
                    "GPU compute specs")
        spec_values = [n.text() for n in specs.find(tag="p")]
        require(len(spec_values) == 3, "Paperspace compute specs changed")
        gpu, ram, cpu = spec_values
        ram_value = match(r"(\d+(?:\.\d+)?\s*GB) RAM", ram, "RAM").group(1)
        cpu_value = int(match(r"(\d+) vCPU", cpu, "vCPU").group(1))
        vram_match = re.fullmatch(r"(\d+(?:\.\d+)?\s*GB) GPU", gpu)
        if vram_match:
            vram = memory(vram_match.group(1))
            raw = title
        else:
            require(gpu.startswith("NVIDIA ") and gpu.endswith(" GPU"), "GPU model spec missing")
            raw = gpu.removesuffix(" GPU")
            stated = re.search(r"-(\d+)G$", title)
            vram = stated.group(1) + "G" if stated else None
        starred = "*" in price
        if starred:
            amount = money(price)
            footnote_pattern = r"\*+\$" + re.escape(f"{amount:.2f}") + r"/hour pricing is for a 3-year commitment\."
            require(re.search(footnote_pattern, hourly.text()), "Conditional GPU price footnote missing")
        model_raw = title if title.startswith("A100-") else raw
        normalized_vram = vram.replace("G", "GB") if vram and vram.endswith("G") else vram
        rows.append(record(
            "paperspace", TIER, URLS[0], fetched_at,
            product="GPU compute Hourly", sku=title,
            gpu_model_raw=model_raw, gpu_model=normalize_gpu(model_raw, normalized_vram),
            vram_raw=vram, vcpu=cpu_value, ram_raw=ram_value,
            price_per_hour=money(price), currency="USD",
            price_type="reserved" if starred else "on_demand", price_scope="instance",
            billing_unit="instance-hour", is_from_price=starred,
            price_conditions="3-year commitment" if starred else None,
        ))
        if title == "H100":
            promo = re.search(r"On-demand pricing for H100 is \$(\d+\.\d+)/hour under our special promo price\.",
                              hourly.text())
            require(promo is not None, "Paperspace H100 on-demand promo footnote changed")
            on_demand = dict(rows[-1])
            on_demand.update(price_per_hour=money(promo.group(1)), price_type="on_demand",
                             is_from_price=True, price_conditions="special promo price")
            rows.append(on_demand)
    return finish(rows)
