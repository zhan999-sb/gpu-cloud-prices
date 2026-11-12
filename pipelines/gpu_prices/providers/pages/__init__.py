"""Independent, network-free pricing-page parsers (Python 3.12 stdlib only).

These modules deliberately do not import the concurrently changing provider
registry, schema, HTTP client, or store. Prices are observations, not inventory
guarantees. B and C records are display-only under the supplied provider spec.

``gpu_count`` is an explicitly stated whole-GPU allocation; ``gpu_fraction``
is 1.0 for such allocations, or an explicitly stated fractional allocation.
Neither is filled from a model name or a per-GPU billing denominator.
``vram_raw`` retains the page's allocation/aggregate memory string.
``observed_at`` and ``availability_observed_at`` are provenance extensions.
"""

from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import math
import re


class ParseError(ValueError):
    """The supplied document no longer satisfies a verified page structure."""


class NotSupported(ParseError):
    """No verified public HTML price structure is currently available."""


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


class Node:
    def __init__(self, tag="", attrs=(), parent=None, start=0):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list[Node | str] = []
        self.start = start
        self.end: int | None = None

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def find(self, *, tag=None, cls=None, **attrs):
        return [n for n in self.walk()
                if (tag is None or n.tag == tag)
                and (cls is None or n.has_class(cls))
                and all(n.attrs.get(k) == v for k, v in attrs.items())]

    def text(self) -> str:
        if self.tag in {"script", "style", "svg"}:
            return ""
        return clean(" ".join(c.text() if isinstance(c, Node) else c
                              for c in self.children))

    def visible(self) -> bool:
        current = self
        while current:
            if current.has_class("hide") or "hidden" in current.attrs:
                return False
            current = current.parent
        return True


class Document(HTMLParser):
    """Small DOM with original source offsets, also used to trim fixtures."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        if not isinstance(html, str) or not html.strip():
            raise ParseError("Expected nonempty HTML")
        self.html = html
        self.line_offsets = [0] + [m.end() for m in re.finditer("\n", html)]
        self.root = Node()
        self.stack = [self.root]
        try:
            self.feed(html)
            self.close()
        except (ValueError, AssertionError) as exc:
            raise ParseError("Malformed HTML") from exc

    def pos(self):
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1], self.pos())
        self.stack[-1].children.append(node)
        if tag in self.VOID:
            node.end = self.pos() + len(self.get_starttag_text())
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop().end = self.pos() + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                # Implicitly closed children do not count as complete tables.
                self.stack[index].end = self.pos() + len(f"</{tag}>")
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def fragment(self, node: Node) -> str:
        require(node.end is not None, f"Unclosed {node.tag}")
        return self.html[node.start:node.end]


def require(condition, message):
    if not condition:
        raise ParseError(message)


def one(nodes, label):
    require(len(nodes) == 1, f"Expected exactly one {label}, found {len(nodes)}")
    require(nodes[0].end is not None, f"Unclosed {label}")
    return nodes[0]


def match(pattern, value, label, flags=0):
    found = re.fullmatch(pattern, clean(value), flags)
    require(found is not None, f"Unexpected {label}: {value!r}")
    return found


def money(value: str) -> float:
    number = match(r"(?:from\s+)?\$?\s*(\d+(?:\.\d+)?)\s*\**", value,
                   "hourly price", re.I).group(1)
    amount = float(number)
    require(math.isfinite(amount) and amount > 0, "Price must be positive and finite")
    return amount


def memory(value: str) -> str:
    match(r"\d+(?:\.\d+)?\s*(?:GB|GiB|MB|MiB|TB|TiB|B)", value, "memory")
    return value


def normalize_gpu(raw: str, vram: str | None = None) -> str:
    """Canonical spelling only; keep HGX/PCIe/SXM/NVL and memory variants."""
    value = clean(raw)
    value = re.sub(r"^Nvidia\s+", "NVIDIA ", value, flags=re.I)
    value = re.sub(r"^NVIDIA Tesla\s+|^Tesla\s+", "NVIDIA ", value)
    value = re.sub(r"\s+GPU$", "", value)
    value = re.sub(r"\bPCIE\b", "PCIe", value, flags=re.I)
    value = re.sub(r"\bNvswitch\b", "NVSwitch", value, flags=re.I)
    value = re.sub(r"\bRTX(?=\d)", "RTX ", value)
    value = re.sub(r"[- ](\d+(?:\.\d+)?)\s*G(?:B)?\b", r" \1GB", value, flags=re.I)
    if not value.startswith("NVIDIA "):
        value = "NVIDIA " + value
    value = re.sub(r"^NVIDIA (A[456]000)\b", r"NVIDIA RTX \1", value)
    if vram and not re.search(r"\d+(?:\.\d+)?GB\b", value):
        value += " " + re.sub(r"\s+", "", vram)
    value = re.sub(r"(\d+)\.0+(?=GB\b)", r"\1", value)
    return value


def record(provider, tier, source_url, fetched_at, **values):
    try:
        stamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "fetched_at must include a timezone")
    except (ValueError, AttributeError, TypeError) as exc:
        raise ParseError("fetched_at must be an ISO-8601 timestamp with timezone") from exc
    result = dict.fromkeys((
        "product", "sku", "gpu_model_raw", "gpu_model", "gpu_count", "gpu_fraction",
        "vram_raw", "vcpu", "ram_raw", "price_per_hour", "currency", "price_type",
        "price_scope", "billing_unit", "minimum_billing_seconds", "effective_from",
        "effective_to", "region", "availability_observed_at",
    ))
    result.update(provider=provider, source_url=source_url, tier=tier,
                  export_allowed=False, is_from_price=False, tax_basis="unknown",
                  availability="unknown", observed_at=fetched_at)
    result.update(values)
    return result


def finish(rows):
    require(bool(rows), "No supported numeric GPU price rows found")
    keys = [(r["product"], r["sku"], r["price_type"]) for r in rows]
    require(len(keys) == len(set(keys)), "Duplicate price identities")
    return rows


def unsupported(html: str, provider: str, reason: str):
    """An unverified adapter cannot accept even a plausible invented table."""
    doc = Document(html)
    if re.search(r"access denied|something went wrong|forbidden", doc.root.text(), re.I):
        raise NotSupported(f"{provider}: HTTP access blocked; {reason}")
    if re.search(r"enable javascript|requires javascript|loading", doc.root.text(), re.I):
        raise NotSupported(f"{provider}: no verified static price structure; {reason}")
    raise ParseError(f"{provider}: no verified HTML price structure; {reason}")
