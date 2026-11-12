"""Parse every GPU table by header, selecting only post-paid hourly prices.

The GPU column explicitly includes counts and fractions. VRAM in this source
is total allocation memory; normalized model capacity is divided by that
explicit allocation, while vram_raw is never rewritten.
"""

from fractions import Fraction
import re
from . import Document, ParseError, finish, match, memory, normalize_gpu, record, require

URLS = ("https://cloud.baidu.com/product-price/gpu.html",)
TIER = "C"


def parse(html: str, fetched_at: str) -> list[dict]:
    doc = Document(html)
    tables = [n for n in doc.root.find(tag="table") if "实例规格" in n.text()]
    require(len(tables) == 4, "Expected Baidu GN5/GN3/vGN3/GN1 tables")
    rows = []
    for table in tables:
        require(table.end is not None, "Unclosed Baidu table")
        trs = table.find(tag="tr")
        require(len(trs) > 1, "Baidu table body missing")
        headers = [n.text() for n in trs[0].find(tag="th")]
        require(headers[:5] == ["实例规格", "GPU", "GPU显存", "CPU", "内存"],
                "Baidu GPU columns changed")
        postpaid = [i for i, h in enumerate(headers) if h.startswith("后付费")]
        require(len(postpaid) == 1, "Baidu post-paid column missing or ambiguous")
        price_index = postpaid[0]
        for tr in trs[1:]:
            require(tr.end is not None, "Unclosed Baidu price row")
            values = [n.text() for n in tr.find(tag="td")]
            require(len(values) == len(headers), "Baidu GPU row column count changed")
            sku, raw, vram, cpu, ram = values[:5]
            require(sku.startswith("bcc."), "Unexpected Baidu instance SKU")
            allocation = match(r"(\d+(?:/\d+)?)\s*\*\s*(.+)", raw, "GPU allocation")
            try:
                fraction = Fraction(allocation.group(1))
            except (ValueError, ZeroDivisionError) as exc:
                raise ParseError("Invalid Baidu GPU fraction") from exc
            require(fraction > 0, "Invalid Baidu GPU allocation")
            require(fraction.denominator == 1 or fraction < 1, "Invalid fractional GPU allocation")
            require(re.match(r"Nvidia\s+\S", allocation.group(2), re.I), "Baidu GPU model missing")
            cpu_count = int(match(r"(\d+)核", cpu, "CPU cores").group(1))
            numeric_price = match(r"(\d+(?:\.\d+)?)\s*(?:元?\s*/\s*小时)?",
                                  values[price_index], "post-paid hourly price")
            require("小时" in headers[price_index] or "小时" in values[price_index],
                    "Baidu post-paid hourly unit missing")
            amount = float(numeric_price.group(1))
            require(amount > 0, "Baidu hourly price must be positive")
            memory(vram)
            # This is arithmetic on an explicit allocation, not SKU inference.
            size = match(r"(\d+(?:\.\d+)?)\s*GB", vram, "GPU memory").group(1)
            capacity = f"{float(size) / float(fraction):g}GB"
            rows.append(record(
                "baidu_gpu", TIER, URLS[0], fetched_at,
                product="GPU云服务器 后付费", sku=sku, gpu_model_raw=raw,
                gpu_model=normalize_gpu(allocation.group(2), capacity),
                gpu_count=int(fraction) if fraction.denominator == 1 else None,
                gpu_fraction=1.0 if fraction.denominator == 1 else float(fraction),
                vram_raw=vram, vcpu=cpu_count, vcpu_raw=cpu, ram_raw=memory(ram),
                price_per_hour=amount, currency="CNY", price_type="on_demand",
                price_scope="instance", billing_unit="instance-hour",
            ))
    return finish(rows)
