"""Workload screens over current, health-checked whole-GPU quotes."""
from html import escape
import re
from types import SimpleNamespace

from .guide_specs import GUIDES
from .regions import eligible
from .reports import week
from .providers.registry import PROVIDERS
from .search import provider_slug
from .workload_specs import (WORKLOADS, SOURCES, GUIDE_LINKS, route, WORKLOAD_CAPACITY,
    GPU_SOFTWARE, VLLM_CUDA_MIN, VLLM_ROCM_TARGETS, PYTORCH_CUDA_TARGETS, PYTORCH_ARM_TARGETS,
    INSTANCE_VRAM_PROVIDERS, PYTORCH_ROCM_TARGETS, capacity_spec, gpu_software, screened_capacity, model_identity, PCIE_ONLY, REVIEWED_MODELS)


def instance_bill(row):
    return row['price_per_hour'] * (row['gpu_count'] if row['price_scope']=='per_gpu' else 1)


def rank(entry):
    p, r = entry
    return r['price_per_gpu_hour'], instance_bill(r), r['gpu_count'], p


def instance_rank(entry):
    return instance_bill(entry[1]), entry[1]['gpu_count'], entry[1]['price_per_gpu_hour'], entry[0]


def arm(row):
    return model_identity(row['gpu_model']) in ('T4G','GH200','GB300','GB300 SXM6') or (row['provider']=='aws' and row['sku'].startswith('g5g.')) or bool(re.search(r'\b(aarch64|arm64|graviton)\b', row.get('cpu_raw') or '', re.I))


def reviewed(row):
    return screened_capacity(row)


def software(row, spec):
    gpu=gpu_software(row['gpu_model'])
    if not gpu: return False
    if spec['kind']=='estimate':
        return (gpu['vendor']=='NVIDIA' and gpu['cc']>=VLLM_CUDA_MIN and (not arm(row) or gpu['cc'] in PYTORCH_ARM_TARGETS)) or (gpu['vendor']=='AMD' and gpu['gfx'] in VLLM_ROCM_TARGETS and not arm(row))
    targets=PYTORCH_ARM_TARGETS if arm(row) else PYTORCH_CUDA_TARGETS
    return (gpu['vendor']=='NVIDIA' and gpu['cc'] in targets) or (gpu['vendor']=='AMD' and gpu['gfx'] in PYTORCH_ROCM_TARGETS and not arm(row))


def snapshot(model_data):
    entries=[];unreviewed=set()
    for model, data in model_data.items():
        for i, row in enumerate(data['offers']):
            if eligible(SimpleNamespace(**row)) and row['availability'] in {'available','unknown'} and model_identity(row['gpu_model']) not in REVIEWED_MODELS:
                unreviewed.add(model_identity(row['gpu_model']))
            capacity=reviewed(row)
            if capacity is not None and eligible(SimpleNamespace(**row)) and row['availability'] in {'available','unknown'}:
                path='/models/'+model.replace('~','~0').replace('/','~1')+'/offers/'+str(i)
                entries.append((path,row,capacity))
    result={}
    for slug,spec in WORKLOADS.items():
        weights=spec['parameters']*spec['bits']/8 if spec['kind']=='estimate' else None
        kv=2*spec['layers']*spec['kv_heads']*spec['head_dim']*spec['kv_bytes']*spec['context']/1e9 if weights is not None else None
        required=weights*1.2+kv if weights is not None else spec['required']
        candidates={g:[] for g in ('single','multi','memory_only')}
        for p,r,v in entries:
            ok=software(r,spec); n=r['gpu_count']
            if v>=required: candidates['single' if ok else 'memory_only'].append((p,r,v))
            if v<required and ok and spec['kind']=='estimate' and n in (2,4,8) and spec['heads']%n==0 and spec['kv_heads']%n==0 and n*v>=required:
                candidates['multi'].append((p,r,v))
        rows={};heads={};selected=[]
        for group, choices in candidates.items():
            # Single-GPU fit minima retain the per-GPU comparison, with a bill
            # and GPU-count tie break. TP compares entire instances.
            groups={}
            for p,r,v in choices:
                key=(r['gpu_model'],r['price_type'],r['gpu_count'] if group=='multi' else 0)
                groups.setdefault(key,[]).append((p,r,v))
            minima=[min(es,key=lambda e:instance_rank(e[:2]) if group=='multi' else rank(e[:2])) for es in groups.values()]
            minima.sort(key=lambda e:instance_rank(e[:2]) if group=='multi' else rank(e[:2]))
            rows[group]=[dict(path=p,offer=r,vram=v,headroom=v*(r['gpu_count'] if group=='multi' else 1)/required) for p,r,v in minima]
            if group!='memory_only': selected += [(p,r) for p,r,v in minima]
            heads[group]={kind:min(((p,r) for p,r,v in choices if r['price_type']==kind),key=instance_rank,default=None) for kind in ('on_demand','spot')}
        # Thin content counts model/type minima once across the two sections.
        count=len({(r['gpu_model'],r['price_type']) for p,r in selected})
        cards={p:r for p,r in selected+[(d['path'],d['offer']) for d in rows['memory_only']]+[e for g in ('single','multi') for e in heads[g].values() if e]}
        current=[r for g in ('single','multi') for p,r,v in candidates[g]]
        result[slug]=dict(required=required,weights=weights,kv=kv,parameters=spec.get('parameters'),
            bytes=spec.get('bits',0)/8,context=spec.get('context'),rows=rows,heads=heads,
            models=sorted({r['gpu_model'] for p,r in selected}),selected=selected,
            cards=sorted(cards.items(),key=instance_rank),count=count,offers=len({p for g in ('single','multi') for p,r,v in candidates[g]}),
            updated=max((r['collected_at'] for r in current),default=None),siblings=[],unreviewed=sorted(unreviewed))
        # "Best" prefers a single-GPU fit; tensor parallel is the answer only when no single GPU fits.
        result[slug]['overall']={kind:heads['single'][kind] or heads['multi'][kind] for kind in ('on_demand','spot')}
    clusters={}
    for slug,d in result.items():
        section='single' if d['rows']['single'] else 'multi'
        signature=(WORKLOADS[slug]['kind'],section,frozenset((x['offer']['gpu_model'],x['offer']['price_type']) for x in d['rows'][section]))
        clusters.setdefault(signature,[]).append(slug)
    for (_,section,_),members in clusters.items():
        strictest=max(members,key=lambda slug:(result[slug]['required'],slug))
        for slug in members:
            d=result[slug]
            if slug!=strictest: d['siblings']=[dict(slug=strictest,sections=[section])]
            d['indexable']=d['count']>=3 and slug==strictest
    return result


def entry_points(tr):
    return '<section data-workload-entry="true"><h2>'+tr.html('workload_entry_title')+'</h2><p>'+tr.html('workload_entry_intro')+'</p><ul>'+''.join(
        '<li><a href="'+tr.language.path(route(slug))+'">'+tr.html('workload_'+s['key']+'_title')+'</a></li>' for slug,s in WORKLOADS.items())+'</ul></section>'


def description(slug,data,tr,day):
    spec=WORKLOADS[slug]
    choices=[e for e in (*data['overall'].values(),*data['heads']['multi'].values()) if e]
    amount=min((instance_bill(e[1]) for e in choices),default=None)
    price=(f'${amount:.2f}' if amount>=1 else f'${amount:.3f}') if choices else tr.text('workload_meta_no_price')
    if choices: price=tr.text('workload_meta_price',price=price)
    return tr.text('workload_description',workload=' '.join(filter(None,(spec['model'],'FP16' if spec.get('bits')==16 else tr.text('workload_precision_4bit') if spec.get('bits')==4 else ''))),
        memory=format(data['required'],'.3f').rstrip('0').rstrip('.'),models=len(data['models']),price=price,date=(data['updated'] or day)[:10])


def render(slug,data,slugs,tr,day,*,stat,table,card):
    spec=WORKLOADS[slug];title=tr.text('workload_'+spec['key']+'_title');base='/workloads/'+slug
    body='<article data-workload="'+slug+'"><h1>'+escape(title)+'</h1>'
    def text(key,tag='p',**values):
        nonlocal body
        body+='<'+tag+' data-search="workload:'+key+'">'+tr.rich('workload_'+key,**values)+'</'+tag+'>'
    def metric(key): return stat(base+'/'+key,data[key],'decimal3' if key in ('kv','required') else 'number')
    text('intro',model=escape(spec['model']));text('basis');text('requirement','h2')
    if spec['kind']=='estimate':
        text('weight_rule',parameters=metric('parameters'),bytes=metric('bytes'),weights=metric('weights'),kv=metric('kv'),memory=metric('required'),context=metric('context'))
        text('weight_caveat')
        text('kv_formula',layers=escape(str(spec['layers'])),heads=escape(str(spec['kv_heads'])),dim=escape(str(spec['head_dim'])),context=escape(str(spec['context'])),kv=escape(format(data['kv'],'.3f').rstrip('0').rstrip('.')))
        if spec['kv_per_1k'] is not None: text('kv_blog')
        if spec['parameters']==13: text('kv_llama2')
        text('architecture',heads=escape(str(spec['heads'])),kv_heads=escape(str(spec['kv_heads'])))
        if spec['bits']==4: text('quant_caveat')
        text('floor_llm')
    else:
        text('whisper_rule',memory=metric('required'));text('floor_whisper')
    text('screening' if spec['kind']=='estimate' else 'screening_whisper')
    if data['updated']: body+='<p>'+tr.html('workload_observed')+tr.label_separator+stat(base+'/updated',data['updated'],'date')+'</p>'
    text('counts',offers=metric('offers'),minima=metric('count'))
    anchors={p:'#quote-'+str(i) for i,(p,r) in enumerate(data['cards'])}
    text('overall','h2')
    for group in ('overall','single','multi') if spec['kind']=='estimate' else ('overall','single'):
        if group!='overall': text(group,'h2')
        for kind in ('on_demand','spot'):
            entry=data['overall'][kind] if group=='overall' else data['heads'][group][kind]
            body+='<p data-search="workload:best:'+group+':'+kind+'">'+tr.html(kind)+tr.label_separator
            if group=='multi' and not entry and data['heads']['single'][kind]:
                body+=tr.html('workload_multi_not_needed',memory=format(data['required'],'.3f').rstrip('0').rstrip('.'))
            elif entry:
                p,r=entry
                body+=tr.rich('workload_best_line_'+('one' if r['gpu_count']==1 else 'other'),
                    model='<a href="'+anchors[p]+'">'+escape(r['gpu_model'])+'</a>',provider=escape(PROVIDERS[r['provider']]['name']),
                    count=stat(p+'/gpu_count',r['gpu_count'],'number'),price=stat(p,r,'instance-money'))
                if group in ('single','overall') and entry is data['heads']['single'][kind] and r['gpu_count']>1: body+=' · '+tr.html('workload_single_bundle',n=format(r['gpu_count'],'g'))
            else: body+=tr.html('not_observed')
            body+='</p>'
    text('minima_note' if spec['kind']=='estimate' else 'minima_note_whisper')
    if data['unreviewed']:
        body+='<details data-workload-unreviewed="true"><summary>'+tr.html('workload_not_reviewed')+'</summary><p data-search="workload:unreviewed-models">'+escape(' · '.join(data['unreviewed']))+'</p></details>'
    for group in ('single','multi','memory_only'):
        if group=='multi' and spec['kind']!='estimate': continue
        text(group+'_table','h2')
        if group=='multi': text('tp_rule')
        table_rows=[]
        for i,d in enumerate(data['rows'][group]):
            p,r=d['path'],d['offer'];name=r['gpu_model'];gpu=gpu_software(name)
            badges=[]
            if arm(r): badges.append(tr.text('workload_arm'))
            if gpu and gpu['vendor']!='NVIDIA': badges.append(tr.text('workload_non_cuda'))
            if not software(r,spec): badges.append(tr.text('workload_below' if gpu else 'workload_unreviewed'))
            target=(('CUDA CC '+format(gpu['cc'],'.1f')) if gpu['cc'] is not None else gpu['gfx']) if gpu else tr.text('workload_unreviewed')
            hardware=escape(gpu['vendor']+' · '+gpu['architecture']+' · '+target) if gpu else escape(target)
            if gpu: hardware='<a href="'+escape(gpu['source'],quote=True)+'">'+hardware+'</a>'
            if group=='multi':
                badges.append(tr.text('workload_tp_row',n=format(r['gpu_count'],'g')))
                if model_identity(name) in PCIE_ONLY: badges.append(tr.text('workload_pcie'))
            if group=='single' and r['gpu_count']>1: badges.append(tr.text('workload_single_bundle',n=format(r['gpu_count'],'g')))
            cells=['<a href="'+tr.language.path('/'+slugs[name]+'/')+'">'+escape(name)+'</a>',
                '<a href="'+escape(capacity_spec(name)['source'],quote=True)+'">'+stat(base+'/rows/'+group+'/'+str(i)+'/vram',d['vram'],'number')+'</a>',
                tr.html(r['price_type']),'<a href="'+anchors[p]+'">'+stat(p+'/price_per_gpu_hour',r['price_per_gpu_hour'],'money')+'</a>',
                stat(p+'/gpu_count',r['gpu_count'],'number'),stat(p,r,'instance-money'),
                tr.html('workload_tight' if d['headroom']<1.25 else 'workload_comfortable'),hardware+(' · '+escape(' · '.join(badges)) if badges else '')]
            table_rows.append('<tr>'+''.join('<td>'+c+'</td>' for c in cells)+'</tr>')
        body+=table([tr.text(k) for k in ('gpu_model','workload_vram','price_type','workload_rate','workload_gpus','workload_instance_price','workload_headroom','workload_support')],table_rows,tr.text('workload_units'))
        if not table_rows:
            if group=='multi' and data['rows']['single']:
                text('multi_not_needed',memory=format(data['required'],'.3f').rstrip('0').rstrip('.'))
            else: text('empty_'+group)
    for sibling in data['siblings']:
        target=sibling['slug'];label=tr.html('workload_'+WORKLOADS[target]['key']+'_title')
        body+='<p data-search="workload:sibling:'+target+'">'+tr.rich('workload_sibling',sibling='<a href="'+tr.language.path(route(target))+'">'+label+'</a>',sections=escape(' / '.join(tr.text('workload_'+g+'_table') for g in sibling['sections'])))+'</p>'
    for i,(p,r) in enumerate(data['cards']):
        body+='<section id="quote-'+str(i)+'">'+card(r,p,tr,model_url=tr.language.path('/'+slugs[r['gpu_model']]+'/'))+'</section>'
    if data['count']<3: text('thin')
    text('check_title','h2');body+='<ul>'
    for key in ('check_memory','check_software','check_cpu','check_host')+(('check_interconnect',) if spec['kind']=='estimate' else ())+('check_cost','check_stock'): text(key,'li')
    body+='</ul>';text('sources','h2')
    body+='<ul data-search="workload:source-list">'+''.join('<li><a href="'+escape(SOURCES[s][1],quote=True)+'">'+tr.html(SOURCES[s][0])+'</a> · <small>'+escape(SOURCES[s][1])+'</small></li>' for s in spec['sources'])+'</ul>'
    text('related','h2')
    body+='<ul>'+''.join('<li><a href="'+tr.language.path('/guides/'+s+'/')+'">'+tr.html('guide_'+GUIDES[s][0]+'_title')+'</a></li>' for s in GUIDE_LINKS)
    body+='<li><a href="'+tr.language.path('/reports/')+'">'+tr.html('report_index_title')+'</a></li><li><a href="'+tr.language.path('/reports/'+week(day)+'/')+'">'+tr.html('report_latest')+'</a></li></ul></article>'
    return title,description(slug,data,tr,day),body,data['count']


def article(slug,title,description,updated,tr,origin):
    url=origin+tr.language.path(route(slug))
    return {'@context':'https://schema.org','@type':'Article','headline':title,
        'description':description,'inLanguage':tr.language.code,'url':url,'mainEntityOfPage':url,
        **({'dateModified':updated} if updated else {}),'citation':[SOURCES[s][1] for s in WORKLOADS[slug]['sources']]}
