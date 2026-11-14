"""Reviewed assumptions and manufacturer configurations, checked 2026-11-14.

Arithmetic uses decimal GB. Published GPU capacity labels are used at face
value, conservatively, without credit for a possible GiB interpretation.
Host-reported names never supply capacity to this screen.
"""
import re

SOURCES = {
    'llama31': ('workload_source_llama31', 'https://huggingface.co/blog/llama31'),
    'llama2': ('workload_source_model', 'https://arxiv.org/html/2307.09288'),
    'architecture': ('workload_source_architecture', 'https://raw.githubusercontent.com/meta-llama/llama-models/main/models/sku_list.py'),
    'llama2_architecture': ('workload_source_llama2_architecture', 'https://raw.githubusercontent.com/meta-pytorch/gpt-fast/main/model.py'),
    'llama_embeddings': ('workload_source_embeddings', 'https://raw.githubusercontent.com/meta-llama/llama-models/main/models/llama3/model.py'),
    'llama2_embeddings': ('workload_source_embeddings', 'https://raw.githubusercontent.com/meta-llama/llama/main/llama/model.py'),
    'memory': ('workload_source_memory', 'https://huggingface.co/docs/accelerate/usage_guides/model_size_estimator'),
    'vllm': ('workload_source_vllm', 'https://docs.vllm.ai/en/stable/getting_started/installation/gpu/'),
    'tp': ('workload_source_tp', 'https://docs.vllm.ai/en/stable/serving/parallelism_scaling/'),
    'pytorch': ('workload_source_pytorch', 'https://github.com/pytorch/pytorch/blob/main/RELEASE.md#pytorch-cuda-support-matrix'),
    'whisper': ('workload_source_whisper', 'https://github.com/openai/whisper#available-models-and-languages'),
    'ada': ('workload_source_ada', 'https://docs.nvidia.com/cuda/ada-compatibility-guide/'),
    'pytorch_rocm': ('workload_source_pytorch_rocm', 'https://github.com/pytorch/pytorch/blob/v2.15.0/.ci/docker/build.sh#L203'),
}
VLLM_CUDA_MIN = 7.5
VLLM_ROCM_GPUS = {
    'MI200s': ('gfx90a',), 'MI300': ('gfx942',), 'MI350': ('gfx950',),
    'Radeon RX 7900 series': ('gfx1100', 'gfx1101'),
    'Radeon RX 9000 series': ('gfx1200', 'gfx1201'),
    'Ryzen AI MAX / AI 300 Series': ('gfx1151', 'gfx1150'),
}
VLLM_ROCM_TARGETS = frozenset(t for targets in VLLM_ROCM_GPUS.values() for t in targets)
# Stable 2.15 / CUDA 13.2.2 release wheels; aarch64 excludes Turing.
PYTORCH_CUDA_TARGETS = frozenset((7.5, 8.0, 8.6, 8.9, 9.0, 10.0, 12.0))
PYTORCH_ARM_TARGETS = frozenset((8.0, 9.0, 10.0, 11.0, 12.0))
PYTORCH_ROCM_TARGETS = frozenset(('gfx90a', 'gfx942', 'gfx950'))
# Reviewed collector schemas: these two public catalogs report instance-total
# GPU memory in vram_raw. It is normalized only to reject over-reporting;
# eligibility always uses WORKLOAD_CAPACITY, never this reported value.
# Verda gpu_memory.description is the instance's '<N>GB GPU RAM' total.
INSTANCE_VRAM_PROVIDERS = frozenset(('aws', 'vultr', 'verda', 'baidu_gpu'))
WORKLOADS = {}
for _size, _model, _heads, _kv_heads, _context, _kv_per_1k in (
        (8, 'Llama 3.1 8B', 32, 8, 8192, .125),
        (13, 'Llama 2 13B', 40, 40, 4096, None),
        (70, 'Llama 3.1 70B', 64, 8, 8192, .313)):
    for _precision, _bits in (('fp16', 16), ('4bit', 4)):
        _slug = ('llama-2-13b' if _size == 13 else f'llama-3-1-{_size}b')+'-'+_precision
        WORKLOADS[_slug] = dict(
            key=f'llm_{_size}b_{_precision}', model=_model, parameters=_size,
            bits=_bits, heads=_heads, kv_heads=_kv_heads, head_dim=128,
            layers=40 if _size == 13 else 32 if _size == 8 else 80,
            context=_context, batch=1, kv_bytes=2, kv_per_1k=_kv_per_1k,
            sources=(('llama2', 'llama2_architecture') if _size == 13 else ('llama31', 'architecture'))+('memory', 'vllm', 'tp', 'pytorch'),
            kind='estimate')
        if _bits == 4:
            # Public Meta architecture + separate input/output matrices verify
            # these inputs without accessing gated HF configs. Preserve P's
            # existing rounded size and the FP16 pages' inputs and arithmetic.
            WORKLOADS[_slug].update(hidden=5120 if _size == 13 else 4096 if _size == 8 else 8192,
                vocab=32000 if _size == 13 else 128256, tie_word_embeddings=False,
                weight_input_sources=('architecture', 'llama2_embeddings' if _size == 13 else 'llama_embeddings'))
            WORKLOADS[_slug]['sources'] += ('architecture', 'llama2_embeddings') if _size == 13 else ('llama_embeddings',)
WORKLOADS['whisper-large'] = dict(key='whisper', model='Whisper large', required=10,
    sources=('whisper', 'pytorch', 'ada', 'pytorch_rocm'), kind='vendor_approximate')

# Exact counts derived from the official configs (including embeddings, output
# projection, RMSNorm and Qwen Q/K/V biases), cross-checked against the official
# BF16 safetensors index total_size / 2. Never use the rounded marketing size.
MODEL_CONFIGS = {
    'qwen2-5-7b': dict(repo='Qwen/Qwen2.5-7B-Instruct', model='Qwen2.5 7B Instruct',
        parameter_count=7615616512, layers=28, heads=28, kv_heads=4, head_dim=128,
        hidden=3584, intermediate=18944, vocab=152064, max_context=32768,
        vendor_bf16=(15.38, 1), vendor_gptq=(6.52, 1), vendor_awq=(6.39, 1),
        same_as=('deepseek_qwen_7b',)),
    'qwen2-5-14b': dict(repo='Qwen/Qwen2.5-14B-Instruct', model='Qwen2.5 14B Instruct',
        parameter_count=14770033664, layers=48, heads=40, kv_heads=8, head_dim=128,
        hidden=5120, intermediate=13824, vocab=152064, max_context=32768,
        vendor_bf16=(29.50, 1), vendor_gptq=(11.36, 1), vendor_awq=(11.22, 1),
        same_as=('deepseek_qwen_14b',)),
    'qwen2-5-32b': dict(repo='Qwen/Qwen2.5-32B-Instruct', model='Qwen2.5 32B Instruct',
        parameter_count=32763876352, layers=64, heads=40, kv_heads=8, head_dim=128,
        hidden=5120, intermediate=27648, vocab=152064, max_context=32768,
        vendor_bf16=(63.72, 1), vendor_gptq=(21.09, 1), vendor_awq=(20.81, 1),
        same_as=('deepseek_qwen_32b',)),
    'qwen2-5-72b': dict(repo='Qwen/Qwen2.5-72B-Instruct', model='Qwen2.5 72B Instruct',
        parameter_count=72706203648, layers=80, heads=64, kv_heads=8, head_dim=128,
        hidden=8192, intermediate=29568, vocab=152064, max_context=32768,
        vendor_bf16=(140.00, 2), vendor_gptq=(42.50, 1), vendor_awq=(42.13, 1), same_as=()),
    'mistral-7b-instruct-v0-3': dict(repo='mistralai/Mistral-7B-Instruct-v0.3', model='Mistral 7B Instruct v0.3',
        parameter_count=7248023552, layers=32, heads=32, kv_heads=8, head_dim=128,
        hidden=4096, intermediate=14336, vocab=32768, max_context=32768, same_as=(),
        vendor_overview='mistral_overview'),
}
SAME_ARCHITECTURE = {
    **{f'deepseek_qwen_{n}b': f'deepseek-ai/DeepSeek-R1-Distill-Qwen-{n}B' for n in (7, 14, 32)},
}
# Anonymous official AWQ / GPTQ-Int4 safetensors metadata, checked 2026-11-14.
# Storage cross-checks only: packing, scale tensors and padding differ from
# the analytical split (the 72B quantized repos pad intermediate_size to 29696).
QWEN_4BIT_INDEX_SIZES = {
    'qwen2-5-7b': (5570747392, 5575277568),
    'qwen2-5-14b': (9980028928, 9988581376),
    'qwen2-5-32b': (19328804864, 19343747072),
    'qwen2-5-72b': (41595518976, 41620750336),
}
for _key, _repo in SAME_ARCHITECTURE.items():
    SOURCES[_key] = ('workload_source_'+_key, f'https://huggingface.co/{_repo}/blob/main/config.json')
SOURCES['qwen_benchmark'] = ('workload_source_qwen_benchmark', 'https://qwen.readthedocs.io/en/v2.5/benchmark/speed_benchmark.html')
SOURCES['qwen_benchmark_code'] = ('workload_source_qwen_benchmark_code', 'https://github.com/QwenLM/Qwen2.5/blob/main/examples/speed-benchmark/speed_benchmark_transformers.py')
SOURCES['mistral_overview'] = ('workload_source_mistral_overview', 'https://docs.mistral.ai/models/mistral-7b-0-3')
for _slug, _config in MODEL_CONFIGS.items():
    _config['tie_word_embeddings'] = False
    if _slug.startswith('qwen'):
        _config['vendor_setting'] = (8192, 1)
    for _suffix, _label, _file in (
            ('card', 'workload_source_card', ''),
            ('config', 'workload_source_config', '/blob/main/config.json'),
            ('index', 'workload_source_index', '/blob/main/model.safetensors.index.json')):
        SOURCES[_slug+'_'+_suffix] = (_label, 'https://huggingface.co/'+_config['repo']+_file)
    for _precision, _bits in (('fp16', 16), ('4bit', 4)):
        WORKLOADS[_slug+'-'+_precision] = dict(_config,
            key=_slug.replace('-', '_')+'_'+_precision, bits=_bits,
            parameters=_config['parameter_count']/1e9, context=min(8192, _config['max_context']),
            batch=1, kv_bytes=2, kv_per_1k=None, kind='estimate',
            sources=tuple(_slug+'_'+s for s in ('card', 'config', 'index'))+
                (('qwen_benchmark', 'qwen_benchmark_code') if _slug.startswith('qwen') else (_config['vendor_overview'],))+
                _config['same_as']+('memory', 'vllm', 'tp', 'pytorch'))
        if _bits == 4:
            WORKLOADS[_slug+'-'+_precision]['weight_input_sources'] = (_slug+'_config',)
            if _slug in QWEN_4BIT_INDEX_SIZES:
                _indices=[]
                for _format,_size in zip(('AWQ','GPTQ'), QWEN_4BIT_INDEX_SIZES[_slug]):
                    _source=_slug+'_'+_format.lower()+'_index'
                    _repo=_config['repo']+'-'+('GPTQ-Int4' if _format=='GPTQ' else _format)
                    SOURCES[_source]=('workload_source_'+_format.lower()+'_index', 'https://huggingface.co/'+_repo+'/blob/main/model.safetensors.index.json')
                    _indices.append((_format,_source,_size))
                WORKLOADS[_slug+'-'+_precision]['checkpoint_sizes']=tuple(_indices)
                WORKLOADS[_slug+'-'+_precision]['sources']+=tuple(s for f,s,n in _indices)

# Shared family membership; labels are localized in every catalogue. The
# independent audit checks these groups and all entry links and labels.
WORKLOAD_FAMILIES = {
    'llama': tuple(s for s in WORKLOADS if s.startswith('llama-')),
    'qwen': tuple(s for s in WORKLOADS if s.startswith('qwen')),
    'mistral': tuple(s for s in WORKLOADS if s.startswith('mistral-')),
    'whisper': ('whisper-large',),
}
CUDA_SOURCE = 'https://developer.nvidia.com/cuda/gpus'
LEGACY_CUDA_SOURCE = 'https://developer.nvidia.com/cuda-legacy-gpus'
AMD_SOURCE = 'https://rocm.docs.amd.com/en/latest/reference/gpu-specs.html'
A100_SOURCE = 'https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a100/pdf/nvidia-a100-datasheet-us-nvidia-1758950-r4-web.pdf'
WORKLOAD_CAPACITY = {}
GPU_SOFTWARE = {}
REVIEWED_MODELS = {}


def _review(names, capacities, source, vendor, architecture, target, software_source=CUDA_SOURCE):
    for name in names:
        REVIEWED_MODELS[name] = tuple(sorted(capacities))
        for capacity in capacities:
            label = f'{name} {capacity}GB'
            WORKLOAD_CAPACITY[label] = dict(gb=capacity, source=source, family=name, maximum=max(capacities))
            GPU_SOFTWARE[label] = dict(vendor=vendor, architecture=architecture,
                cc=target if vendor == 'NVIDIA' else None,
                gfx=target if vendor == 'AMD' else None, source=software_source)


_review(('A100', 'A100 PCIe', 'A100 SXM', 'A100 SXM4', 'A100 NVSwitch', 'HGX A100', 'DGX A100'),
        (40, 80), A100_SOURCE, 'NVIDIA', 'Ampere', 8.0)
_review(('H100', 'H100 PCIe', 'H100 SXM', 'HGX H100'), (80,),
        'https://www.nvidia.com/en-us/data-center/h100/', 'NVIDIA', 'Hopper', 9.0)
_review(('H100 NVL',), (94,), 'https://www.nvidia.com/en-us/data-center/h100/', 'NVIDIA', 'Hopper', 9.0)
_review(('H200', 'H200 SXM5', 'H200 NVL'), (141,),
        'https://www.nvidia.com/en-us/data-center/h200/', 'NVIDIA', 'Hopper', 9.0)
_review(('B200', 'B200 SXM6', 'HGX B200'), (180,),
        'https://www.nvidia.com/en-us/data-center/dgx-b200/', 'NVIDIA', 'Blackwell', 10.0)
_review(('L40S',), (48,), 'https://www.nvidia.com/en-us/data-center/l40s/', 'NVIDIA', 'Ada Lovelace', 8.9)
_review(('L4',), (24,), 'https://www.nvidia.com/en-us/data-center/l4/', 'NVIDIA', 'Ada Lovelace', 8.9)
_review(('T4',), (16,), 'https://www.nvidia.com/en-us/data-center/tesla-t4/', 'NVIDIA', 'Turing', 7.5)
_review(('T4G',), (16,), 'https://aws.amazon.com/ec2/instance-types/g5g/', 'NVIDIA', 'Turing', 7.5)
_review(('A10', 'A10 PCIe'), (24,),
        'https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/a10/pdf/a10-datasheet.pdf', 'NVIDIA', 'Ampere', 8.6)
_review(('A10G',), (24,), 'https://aws.amazon.com/ec2/instance-types/g5/', 'NVIDIA', 'Ampere', 8.6)
_review(('RTX 4090',), (24,), 'https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4090/', 'NVIDIA', 'Ada Lovelace', 8.9)
_review(('RTX 5090',), (32,), 'https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/', 'NVIDIA', 'Blackwell', 12.0)
_review(('V100', 'V100 PCIe', 'V100 SXM2'), (16, 32),
        'https://images.nvidia.com/content/technologies/volta/pdf/tesla-volta-v100-datasheet-letter-fnl-web.pdf', 'NVIDIA', 'Volta', 7.0, LEGACY_CUDA_SOURCE)
_review(('Tesla M60',), (8,), 'https://images.nvidia.com/content/pdf/tesla/tesla-m60-product-brief.pdf', 'NVIDIA', 'Maxwell', 5.2, LEGACY_CUDA_SOURCE)
_review(('Radeon Pro V520',), (8,), 'https://aws.amazon.com/ec2/instance-types/g4/', 'AMD', 'RDNA', 'gfx1011',
        'https://rocm.docs.amd.com/projects/llvm-project/en/latest/LLVM/llvm/html/AMDGPUUsage.html')
_review(('MI300X',), (192,), 'https://www.amd.com/en/products/accelerators/instinct/mi300/mi300x.html', 'AMD', 'CDNA 3', 'gfx942', AMD_SOURCE)

# Coverage of the complete 2026-11-13 provider snapshot, including models below
# the software floor. Aliases describe the same silicon, never host VRAM mods.
_review(('A2',), (16,), 'https://www.nvidia.com/content/dam/en-zz/solutions/data-center/a2/pdf/a2-datasheet.pdf', 'NVIDIA', 'Ampere', 8.6)
_review(('A30',), (24,), 'https://www.nvidia.com/en-us/data-center/products/a30-gpu/', 'NVIDIA', 'Ampere', 8.0)
_review(('A40',), (48,), 'https://www.nvidia.com/en-us/data-center/a40/', 'NVIDIA', 'Ampere', 8.6)
_review(('L40',), (48,), 'https://www.nvidia.com/en-us/data-center/l40/', 'NVIDIA', 'Ada Lovelace', 8.9)
_review(('B200 CC SXM6',), (180,), 'https://www.nvidia.com/en-us/data-center/dgx-b200/', 'NVIDIA', 'Blackwell', 10.0)
_review(('B300', 'B300 SXM6', 'B300 CC SXM6'), (288,), 'https://docs.nvidia.com/dgx/dgxb300-user-guide/introduction-to-dgxb300.html', 'NVIDIA', 'Blackwell', 10.3)
_review(('GB300', 'GB300 SXM6'), (288,), 'https://www.nvidia.com/en-us/data-center/gb300-nvl72/', 'NVIDIA', 'Blackwell', 10.3)
_review(('GH200',), (96, 144), 'https://dam-cdn.nvd.orangelogic.com/AssetLink/tkno03fa645gb7f33w60rf200d330420.pdf', 'NVIDIA', 'Hopper', 9.0)
_review(('MI325X',), (256,), 'https://www.amd.com/en/products/accelerators/instinct/mi300/mi325x.html', 'AMD', 'CDNA 3', 'gfx942', AMD_SOURCE)
_review(('MI355X',), (288,), 'https://www.amd.com/en/products/accelerators/instinct/mi350/mi355x.html', 'AMD', 'CDNA 4', 'gfx950', AMD_SOURCE)
for _name, _caps in (('RTX A2000',(6,12)), ('RTX A4000',(16,)), ('RTX A5000',(24,)), ('RTX A6000',(48,))):
    _review((_name,), _caps, 'https://www.nvidia.com/en-us/design-visualization/desktop-graphics/', 'NVIDIA', 'Ampere', 8.6)
for _name, _cap in (('RTX 4000 Ada',20), ('RTX 6000 Ada',48)):
    _review((_name,), (_cap,), 'https://www.nvidia.com/en-us/design-visualization/desktop-graphics/', 'NVIDIA', 'Ada Lovelace', 8.9)
for _names, _cap in (
        (('RTX PRO 2000 Blackwell',),16), (('RTX PRO 4000 Blackwell',),24),
        (('RTX PRO 4500', 'RTX PRO 4500 Blackwell'),32),
        (('RTX PRO 6000', 'RTX PRO 6000 CC', 'RTX PRO 6000 Blackwell',
          'RTX PRO 6000 Blackwell Server Edition', 'RTX PRO 6000 Blackwell Workstation',
          'RTX PRO 6000 Blackwell Workstation Edition', 'RTX PRO 6000 Blackwell MaxQ Works',
          'RTX PRO 6000 Blackwell Max-Q Workstation Edition'),96)):
    _review(_names, (_cap,), 'https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/', 'NVIDIA', 'Blackwell', 12.0)

_GEFORCE = 'https://www.nvidia.com/en-us/geforce/graphics-cards/compare/'
for _name, _caps in (
        ('RTX 3050',(6,8)), ('RTX 3060',(8,12)), ('RTX 3060 Ti',(8,)),
        ('RTX 3070',(8,)), ('RTX 3070 Ti',(8,)), ('RTX 3080',(10,12)),
        ('RTX 3080 Ti',(12,)), ('RTX 3090',(24,)), ('RTX 3090 Ti',(24,))):
    _review((_name,), _caps, _GEFORCE, 'NVIDIA', 'Ampere', 8.6)
for _name, _caps in (
        ('RTX 4060',(8,)), ('RTX 4060 Ti',(8,16)), ('RTX 4070',(12,)),
        ('RTX 4070 SUPER',(12,)), ('RTX 4070 Ti',(12,)), ('RTX 4070 Ti SUPER',(16,)),
        ('RTX 4080',(16,)), ('RTX 4080 SUPER',(16,))):
    _review((_name,), _caps, _GEFORCE, 'NVIDIA', 'Ada Lovelace', 8.9)
for _name, _caps in (
        ('RTX 5060',(8,)), ('RTX 5060 Ti',(8,16)), ('RTX 5070',(12,)),
        ('RTX 5070 Ti',(16,)), ('RTX 5080',(16,))):
    _review((_name,), _caps, _GEFORCE, 'NVIDIA', 'Blackwell', 12.0)
_review(('RTX 5090 D',), (32,), 'https://www.nvidia.cn/geforce/news/rtx-50-series-graphics-cards-gpu-laptop-announcements/', 'NVIDIA', 'Blackwell', 12.0)
_review(('RTX 5090 D v2',), (24,), 'https://www.nvidia.cn/geforce/graphics-cards/50-series/rtx-5090-d-v2/', 'NVIDIA', 'Blackwell', 12.0)
_review(('RTX PRO 6000 Blackwell Server Edition',), (96,), 'https://www.nvidia.com/en-us/data-center/rtx-pro-6000-blackwell-server-edition/', 'NVIDIA', 'Blackwell', 12.0)
for _name, _cap in (('RTX 2060 SUPER',8), ('RTX 2070',8), ('RTX 2070 SUPER',8),
        ('RTX 2080',8), ('RTX 2080 SUPER',8), ('RTX 2080 Ti',11)):
    _review((_name,), (_cap,), _GEFORCE, 'NVIDIA', 'Turing', 7.5)
_review(('Quadro RTX 6000',), (24,), 'https://www.nvidia.com/en-us/design-visualization/quadro/rtx-6000/', 'NVIDIA', 'Turing', 7.5)
for _name in ('GTX 1660 SUPER', 'GTX 1660 Ti'):
    _review((_name,), (6,), _GEFORCE, 'NVIDIA', 'Turing', 7.5)
for _name, _cap, _source in (
        ('GT 1030',2,'https://www.nvidia.com/en-us/geforce/graphics-cards/gt-1030/specifications/'),
        ('GTX 1050',3,'https://storage-asset.msi.com/datasheet/vga/global/GeForce-GTX-1050-3GT-OC.pdf'),
        ('GTX 1060',6,_GEFORCE),
        ('GTX 1070',8,'https://storage-asset.msi.com/datasheet/vga/global/GeForce-GTX-1070-Founders-Edition.pdf'),
        ('GTX 1070 Ti',8,'https://storage-asset.msi.com/datasheet/vga/global/GeForce-GTX-1070-Ti-GAMING-8G.pdf'),
        ('GTX 1080 Ti',11,'https://storage-asset.msi.com/datasheet/vga/global/GeForce-GTX-1080-Ti-GAMING-X-11G.pdf')):
    _review((_name,), (_cap,), _source, 'NVIDIA', 'Pascal', 6.1, LEGACY_CUDA_SOURCE)
_review(('P40',), (24,), 'https://images.nvidia.com/content/pdf/tesla/184427-Tesla-P40-Datasheet-NV-Final-Letter-Web.pdf', 'NVIDIA', 'Pascal', 6.1, LEGACY_CUDA_SOURCE)
_review(('Tesla M40',), (12,24), 'https://images.nvidia.com/content/tesla/pdf/78071_Tesla_M40_24GB_Print_Datasheet_LR.PDF', 'NVIDIA', 'Maxwell', 5.2, LEGACY_CUDA_SOURCE)
_review(('Tesla K80',), (12,), 'https://www.nvidia.com/content/dam/en-zz/Solutions/Data-Center/tesla-product-literature/TeslaK80-datasheet.pdf', 'NVIDIA', 'Kepler', 3.7, LEGACY_CUDA_SOURCE)
_review(('GRID K520',), (4,), 'https://developer.nvidia.com/sites/default/files/akamai/gameworks/events/gdc14/GDC_14_Cloud_Gaming_With_GRID_Technologies.pdf', 'NVIDIA', 'Kepler', 3.0,
        'https://docs.nvidia.com/cuda/archive/11.8.0/kepler-tuning-guide/index.html')

PCIE_ONLY = frozenset(('L4', 'L40S', 'L40', 'T4', 'T4G', 'A10', 'A10G'))
_CAPACITY = re.compile(r'(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(?:GB|GiB)\b', re.I)


def reported_capacities(value):
    """Only separated capacity tokens; A100SXM440GB is a model token."""
    return tuple(float(v) for v in _CAPACITY.findall(value or ''))


def model_identity(label):
    return re.sub(r'\s+\d+(?:\.\d+)?\s*(?:GB|GiB)$', '', label, flags=re.I)


def capacity_spec(label):
    if re.search(r'Laptop|\bCMP\b|P10[0-9]-?100', label, re.I): return None
    if label in WORKLOAD_CAPACITY: return WORKLOAD_CAPACITY[label]
    family = model_identity(label)
    sizes = REVIEWED_MODELS.get(family)
    if not sizes: return None
    reports = reported_capacities(label)
    reported = reports[-1] if reports else sizes[0]
    size = next((v for v in sizes if v >= reported), None)
    return WORKLOAD_CAPACITY.get(f'{family} {size}GB') if size is not None else None


def gpu_software(label):
    if label in WORKLOAD_CAPACITY: return GPU_SOFTWARE.get(label)
    cap = capacity_spec(label)
    return GPU_SOFTWARE.get(f"{cap['family']} {cap['gb']}GB") if cap else None


def screened_capacity(row):
    """Spec lookup by identity, conservative reported capacity, no enlargement."""
    cap = capacity_spec(row['gpu_model'])
    if cap is None: return None
    values = list(reported_capacities(row['gpu_model']))
    for field in ('vram_raw', 'gpu_model_raw'):
        divisor = row['gpu_count'] if field == 'vram_raw' and row['provider'] in INSTANCE_VRAM_PROVIDERS else 1
        if divisor is None or divisor <= 0: return None
        values.extend(v/divisor for v in reported_capacities(row.get(field)))
    if any(v > cap['gb'] for v in values): return None
    return min([cap['gb']] + values)
GUIDE_LINKS = ('choose-gpu-cloud-llm-inference', 'per-gpu-hour-vs-instance', 'spot-vs-on-demand')


def route(slug):
    return '/workloads/best-cloud-gpu-for-'+slug+'/'
