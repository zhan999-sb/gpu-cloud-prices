"""Phase 1 policy registry. D providers deliberately have no collector."""
PROVIDERS = {
    "azure": {"name": "Azure", "tier": "A", "enabled": True, "note": "note_azure", "source": "https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices"},
    "linode": {"name": "Linode / Akamai", "tier": "A", "enabled": True, "note": "note_linode", "source": "https://techdocs.akamai.com/linode-api/reference/get-linode-types"},
    "scaleway": {"name": "Scaleway", "tier": "A", "enabled": True, "note": "note_scaleway", "source": "https://www.scaleway.com/en/developers/api/product-catalog/public-catalog"},
    "verda": {"name": "Verda / DataCrunch", "tier": "A", "enabled": True, "note": "note_verda", "source": "https://docs.verda.com/welcome-to-verda/release-notes/verda-api-changes/"},
    "vultr": {"name": "Vultr", "tier": "A", "enabled": False, "note": "todo_vultr", "source": "https://api.vultr.com/v2/plans"},
    "oracle": {"name": "Oracle Cloud", "tier": "A", "enabled": False, "note": "todo_oracle", "source": "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"},
    "ovh": {"name": "OVHcloud", "tier": "A", "enabled": False, "note": "todo_ovh", "source": "https://eu.api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=FR"},
    "vast": {"name": "Vast.ai", "tier": "D", "enabled": False, "note": "link_only_note", "source": "https://vast.ai/terms"},
    "together": {"name": "Together", "tier": "D", "enabled": False, "note": "link_only_note", "source": "https://www.together.ai/terms-of-service"},
}
