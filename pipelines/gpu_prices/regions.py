"""Reviewed Azure location classifications, separate from prices.

Sources checked 2026-11-12:
https://learn.microsoft.com/en-us/azure/reliability/regions-list
https://learn.microsoft.com/en-us/troubleshoot/azure/general/region-access-request-process
https://learn.microsoft.com/en-us/azure/extended-zones/overview
https://learn.microsoft.com/en-us/azure/extended-zones/request-access
https://learn.microsoft.com/en-us/azure/postgresql/troubleshoot/how-to-resolve-capacity-errors

The regions-list restriction icon supplies the reserved-access list. West India
is NOT marked reserved in the current list: exclude it conservatively, along
with the legacy Jio locations, as an explicit comparison policy (the program
regions documentation describes subscription-specific restrictions). Do not
claim all subscriptions require approval there. Sovereign locations are outside
the public-commercial comparison. ATT/SGX/metro identifiers are edge locations;
new ATT zones inherit that classification. Unknown locations remain standard
as requested, with a warning for review, never a silent inferred restriction.
"""

import logging

LOG = logging.getLogger(__name__)
_UNKNOWN_LOGGED = set()
REGION_SOURCES = (
    "https://learn.microsoft.com/en-us/azure/reliability/regions-list",
    "https://learn.microsoft.com/en-us/troubleshoot/azure/general/region-access-request-process",
    "https://learn.microsoft.com/en-us/azure/extended-zones/request-access",
    "https://learn.microsoft.com/en-us/azure/postgresql/troubleshoot/how-to-resolve-capacity-errors",
)

# Data table: region -> classification. Names not present here are logged.
REGIONS = dict.fromkeys("""australiacentral australiaeast australiasoutheast
austriaeast belgiumcentral brazilsouth canadacentral canadaeast centralindia
centralus chilecentral denmarkeast eastasia eastus eastus2 francecentral
germanywestcentral indiasouthcentral indonesiacentral israelcentral italynorth
japaneast japanwest koreacentral koreasouth malaysiawest mexicocentral
newzealandnorth northcentralus northeurope norwayeast polandcentral qatarcentral
southafricanorth southcentralus southeastasia southindia spaincentral
swedencentral switzerlandnorth taiwannorth uaenorth uksouth ukwest westcentralus
westeurope westus westus2 westus3""".split(), "standard")
REGIONS.update(dict.fromkeys("""australiacentral2 brazilsoutheast francesouth
germanynorth norwaywest southafricawest swedensouth switzerlandwest
taiwannorthwest uaecentral jioindiacentral jioindiawest westindia
usgovarizona usgovtexas usgovvirginia usgoviowa usdodcentral usdodeast
chinaeast chinaeast2 chinaeast3 chinanorth chinanorth2 chinanorth3""".split(), "restricted"))
REGIONS.update(dict.fromkeys("""attatlanta1 attdallas1 attdetroit1 attnewyork1
sgxsingapore1 microsoftvancouver1 losangeles losangeles1 perth portland""".split(), "edge"))
# Official Linode /regions site_type=core, verified 2026-11-12; Scaleway zones:
# https://www.scaleway.com/en/docs/console/account/reference-content/products-availability/
REGIONS.update(dict.fromkeys("""gb-lon se-sto es-mad in-maa jp-osa it-mil us-mia
id-cgk us-lax nl-ams au-mel in-bom-2 de-fra-2 sg-sin-2 jp-tyo-3 us-iad-2
fr-par-2 br-gru us-sea fr-par us-ord us-iad ap-southeast ca-central ap-west
us-central ap-northeast eu-central ap-south eu-west us-east us-southeast us-west
fr-par-1 fr-par-3 nl-ams-1 nl-ams-2 nl-ams-3 pl-waw-1 pl-waw-2 pl-waw-3""".split(), "standard"))


def region_class(region: str | None) -> str:
    if not region:
        return "unknown_region"
    key = region.casefold()
    if key.startswith("att"):
        return "edge"
    if key not in REGIONS and key not in _UNKNOWN_LOGGED:
        _UNKNOWN_LOGGED.add(key)
        LOG.warning("Unknown Azure region defaults to standard; review classification: %s", region)
    return REGIONS.get(key, "standard")


def eligible(offer, *, currency="USD") -> bool:
    return (offer.currency == currency and offer.gpu_count is not None and offer.gpu_count >= 1 and float(offer.gpu_count).is_integer()
            and offer.gpu_fraction == 1 and not offer.is_from_price and offer.tax_basis != "included"
            and offer.price_scope in {"instance", "per_gpu"}
            and offer.price_type in ("on_demand", "spot")
            and region_class(offer.region) in {"standard", "unknown_region"})
