"""Curated exact Azure ARM size mappings. No regex/spec inference or API scraping.

Sources checked 2026-11-12. GPU model memory is per *whole* GPU; fractional
allocations do not expose all that VRAM. Source URLs also appear on the site.
This is deliberately a curated subset, not a claim of exhaustive SKU coverage.
"""

from dataclasses import dataclass
import logging

LOG = logging.getLogger(__name__)
DOCS = "https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/"


@dataclass(frozen=True, slots=True)
class GpuSpec:
    gpu_model: str
    gpu_count: float
    vcpu: int
    ram_gb: float
    source_url: str


AZURE_SKUS: dict[str, GpuSpec] = {}


def _family(page: str, model: str, rows: tuple) -> None:
    for sku, count, vcpu, ram in rows:
        AZURE_SKUS[sku] = GpuSpec(model, count, vcpu, ram, DOCS + page)


# NCasT4 v3: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncast4v3-series
_family("ncast4v3-series", "T4 16GB", (
    ("Standard_NC4as_T4_v3", 1, 4, 28),
    ("Standard_NC8as_T4_v3", 1, 8, 56),
    ("Standard_NC16as_T4_v3", 1, 16, 110),
    ("Standard_NC64as_T4_v3", 4, 64, 440),
))
# NCv3: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncv3-series
_family("ncv3-series", "V100 16GB", (
    ("Standard_NC6s_v3", 1, 6, 112),
    ("Standard_NC12s_v3", 2, 12, 224),
    ("Standard_NC24s_v3", 4, 24, 448),
    ("Standard_NC24rs_v3", 4, 24, 448),
))
# NDv2: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndv2-series
_family("ndv2-series", "V100 32GB", (("Standard_ND40rs_v2", 8, 40, 672),))
# NC A100 v4: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nca100v4-series
_family("nca100v4-series", "A100 80GB", (
    ("Standard_NC24ads_A100_v4", 1, 24, 220),
    ("Standard_NC48ads_A100_v4", 2, 48, 440),
    ("Standard_NC96ads_A100_v4", 4, 96, 880),
))
# ND A100 v4: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndasra100v4-series
_family("ndasra100v4-series", "A100 40GB", (("Standard_ND96asr_v4", 8, 96, 900),))
# NDm A100 v4: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndma100v4-series
_family("ndma100v4-series", "A100 80GB", (("Standard_ND96amsr_A100_v4", 8, 96, 1900),))
# ND H100 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndh100v5-series
_family("ndh100v5-series", "H100 SXM 80GB", (("Standard_ND96isr_H100_v5", 8, 96, 1900),))
# ND H200 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nd-h200-v5-series
_family("nd-h200-v5-series", "H200 141GB", (("Standard_ND96isr_H200_v5", 8, 96, 1850),))
# NCads H100 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ncadsh100v5-series
_family("ncadsh100v5-series", "H100 NVL 94GB", (
    ("Standard_NC40ads_H100_v5", 1, 40, 320),
    ("Standard_NC80adis_H100_v5", 2, 80, 640),
))
# NCCads H100 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nccadsh100v5-series
_family("nccadsh100v5-series", "H100 NVL 94GB", (("Standard_NCC40ads_H100_v5", 1, 40, 320),))
# NVads A10 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsa10v5-series
_family("nvadsa10v5-series", "A10 24GB", (
    ("Standard_NV6ads_A10_v5", 1 / 6, 6, 55),
    ("Standard_NV12ads_A10_v5", 1 / 3, 12, 110),
    ("Standard_NV18ads_A10_v5", 1 / 2, 18, 220),
    ("Standard_NV36ads_A10_v5", 1, 36, 440),
    ("Standard_NV36adms_A10_v5", 1, 36, 880),
    ("Standard_NV72ads_A10_v5", 2, 72, 880),
))
# ND MI300X v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/ndmi300xv5-series
_family("ndmi300xv5-series", "MI300X 192GB", (("Standard_ND96isr_MI300X_v5", 8, 96, 1850),))
# NVads V710 v5: https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/gpu-accelerated/nvadsv710-v5-series
_family("nvadsv710-v5-series", "Radeon Pro V710 24GB", (
    ("Standard_NV4ads_V710_v5", 1 / 6, 4, 16),
    ("Standard_NV8ads_V710_v5", 1 / 3, 8, 32),
    ("Standard_NV12ads_V710_v5", 1 / 2, 12, 64),
    ("Standard_NV24ads_V710_v5", 1, 24, 128),
    ("Standard_NV28adms_V710_v5", 1, 28, 160),
))
# No Microsoft Azure NC/ND/NV L40S size was verified. Never invent an L40S alias.
# Retired NC/NV generations and newly introduced, unreviewed families stay unknown.


def lookup_azure_sku(sku: str) -> GpuSpec | None:
    spec = AZURE_SKUS.get(sku)
    if spec is None:
        LOG.warning("Unknown Azure GPU SKU skipped: %s", sku)
    return spec
