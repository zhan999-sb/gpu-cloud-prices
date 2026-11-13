"""Reviewed editorial identities and official references; never price inputs."""

GUIDES = {
    'choose-gpu-cloud-llm-inference': ('choose', ('llama', 'memory', 'cache')),
    'spot-vs-on-demand': ('spot', ('aws_spot', 'azure_spot', 'gcp_spot')),
    'per-gpu-hour-vs-instance': ('billing', ('aws_billing', 'gcp_gpu')),
    'gpu-cloud-price-glossary': ('glossary', ('aws_billing', 'aws_reserved', 'gcp_spot', 'gcp_gpu', 'regions')),
    'cheapest-gpu-per-vram-gb': ('value', ('memory',)),
}
SOURCES = {
    'llama': ('Meta: Llama model card', 'https://huggingface.co/meta-llama/Llama-2-7b-hf'),
    'memory': ('Hugging Face: model memory estimator', 'https://huggingface.co/docs/accelerate/usage_guides/model_size_estimator'),
    'cache': ('Hugging Face: cache strategies', 'https://huggingface.co/docs/transformers/kv_cache'),
    'aws_spot': ('AWS: Spot interruption notices', 'https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html'),
    'azure_spot': ('Microsoft: Azure Spot VMs', 'https://learn.microsoft.com/en-us/azure/virtual-machines/spot-vms'),
    'gcp_spot': ('Google Cloud: Spot VMs', 'https://docs.cloud.google.com/compute/docs/instances/spot'),
    'aws_billing': ('AWS: on-demand billing', 'https://aws.amazon.com/ec2/pricing/on-demand/'),
    'aws_reserved': ('AWS: Reserved Instances', 'https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-reserved-instances.html'),
    'gcp_gpu': ('Google Cloud: GPU pricing', 'https://cloud.google.com/products/compute/gpus-pricing'),
    'regions': ('Microsoft: region access', 'https://learn.microsoft.com/en-us/troubleshoot/azure/general/region-access-request-process'),
}
# Nominal model-card sizes, not an exact tensor count or measured runtime VRAM.
# Persisted as reference rows, separate from observations, on writable DB opens.
REFERENCE_MODELS = tuple((name, size, SOURCES['llama'][1]) for name, size in
                         (('Llama 2 7B', 7), ('Llama 2 13B', 13), ('Llama 2 70B', 70)))


def reference_rows(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='guide_reference_models'").fetchone():
        return []  # Old read-only databases remain supported, without invented facts.
    rows = [tuple(r) for r in connection.execute(
        'SELECT model,parameters_billions,source_url FROM guide_reference_models ORDER BY parameters_billions,model')]
    if rows and rows != list(REFERENCE_MODELS):
        raise ValueError('guide reference database differs from reviewed official model-card facts')
    return rows
