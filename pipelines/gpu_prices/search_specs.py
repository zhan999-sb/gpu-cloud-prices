"""Reviewed manufacturer reference configurations, checked 2026-11-13.

These are hardware specifications, never inferred from a provider's raw name.
TFLOPS below are the published *sparse* Tensor Core peaks. GeForce entries
explicitly use FP32 accumulation. No workload-performance claims follow.
"""

SPECS = {
    'H100 SXM 80GB': dict(vram=80, bandwidth=3350, fp16=1979, bf16=1979,
        condition='spec_sparse', source='https://www.nvidia.com/en-us/data-center/h100/'),
    'A100 SXM4 80GB': dict(vram=80, bandwidth=2039, fp16=624, bf16=624,
        condition='spec_sparse', source='https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a100/pdf/nvidia-a100-datasheet-us-nvidia-1758950-r4-web.pdf'),
    'L40S 48GB': dict(vram=48, bandwidth=864, fp16=733, bf16=733,
        condition='spec_sparse', source='https://www.nvidia.com/en-us/data-center/l40s/'),
    'RTX 4090 24GB': dict(vram=24, bandwidth=1008, fp16=330.4, bf16=330.4,
        condition='spec_sparse_fp32', source='https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf'),
    'RTX 5090 32GB': dict(vram=32, bandwidth=1792, fp16=419, bf16=419,
        condition='spec_sparse_fp32', source='https://images.nvidia.com/aem-dam/Solutions/geforce/blackwell/nvidia-rtx-blackwell-gpu-architecture.pdf'),
    'H200 SXM5 141GB': dict(vram=141, bandwidth=4800, fp16=1979, bf16=1979,
        condition='spec_sparse_preliminary', source='https://www.nvidia.com/en-us/data-center/h200/'),
}
PAIRS = {'h100-vs-a100': ('H100', 'A100'), 'a100-vs-l40s': ('A100', 'L40S'),
         'rtx-4090-vs-rtx-5090': ('RTX 4090', 'RTX 5090'), 'h100-vs-h200': ('H100', 'H200')}
REFERENCES = {'H100': 'H100 SXM 80GB', 'A100': 'A100 SXM4 80GB', 'L40S': 'L40S 48GB',
              'RTX 4090': 'RTX 4090 24GB', 'RTX 5090': 'RTX 5090 32GB', 'H200': 'H200 SXM5 141GB'}
