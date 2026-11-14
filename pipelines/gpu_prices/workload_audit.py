"""Independent SQL-derived workload gate. No renderer imports or oracle calls."""
from datetime import date, datetime
from html import unescape
from pathlib import Path
import re

from .workload_specs import (WORKLOADS, WORKLOAD_FAMILIES, SAME_ARCHITECTURE, SOURCES, GUIDE_LINKS, route, WORKLOAD_CAPACITY,
    GPU_SOFTWARE, VLLM_CUDA_MIN, VLLM_ROCM_TARGETS, PYTORCH_CUDA_TARGETS, PYTORCH_ARM_TARGETS,
    INSTANCE_VRAM_PROVIDERS, PYTORCH_ROCM_TARGETS, capacity_spec, gpu_software, screened_capacity, model_identity, PCIE_ONLY, REVIEWED_MODELS)
from .providers.registry import PROVIDERS


def _bill(r):
    return r['price_per_hour']*(r['gpu_count'] if r['price_scope']=='per_gpu' else 1)


def _arm(r):
    return (model_identity(r['gpu_model']) in ('T4G','GH200','GB300','GB300 SXM6') or r['provider']=='aws' and r['sku'].startswith('g5g.') or
            re.search(r'\b(aarch64|arm64|graviton)\b',r.get('cpu_raw') or '',re.I) is not None)


def _support(r,spec):
    g=gpu_software(r['gpu_model'])
    if g is None: return False
    if spec['kind']=='estimate':
        return (g['vendor']=='NVIDIA' and g['cc']>=VLLM_CUDA_MIN and (not _arm(r) or g['cc'] in PYTORCH_ARM_TARGETS)) or (g['vendor']=='AMD' and g['gfx'] in VLLM_ROCM_TARGETS and not _arm(r))
    return (g['vendor']=='NVIDIA' and g['cc'] in (PYTORCH_ARM_TARGETS if _arm(r) else PYTORCH_CUDA_TARGETS)) or (g['vendor']=='AMD' and g['gfx'] in PYTORCH_ROCM_TARGETS and not _arm(r))


def _order(e,multi=False):
    p,r=e[:2]
    return (_bill(r),r['gpu_count'],r['price_per_gpu_hour'],p) if multi else (r['price_per_gpu_hour'],_bill(r),r['gpu_count'],p)


def snapshot(facts):
    from .audit import _eligible
    entries=[];unreviewed=set()
    for name,model in facts['models'].items():
        for i,r in enumerate(model['offers']):
            if not _eligible(r) or r['availability'] not in ('unknown','available'): continue
            if model_identity(name) not in REVIEWED_MODELS: unreviewed.add(model_identity(name))
            capacity=screened_capacity(r)
            if capacity is None: continue
            entries.append(('/models/'+name.replace('~','~0').replace('/','~1')+'/offers/'+str(i),r,capacity))
    answer={}
    for slug,s in WORKLOADS.items():
        unquantized=unquantized_weights=quantized_weights=None
        if s['kind']=='estimate':
            weight=s['parameters']*(s['bits']/8)
            if s['bits']==4:
                unquantized=s['hidden']*s['vocab']
                if not s['tie_word_embeddings']: unquantized*=2
                unquantized_weights=2*unquantized/10**9
                quantized_weights=(s['parameters']*10**9-unquantized)/2/10**9
                weight=unquantized_weights+quantized_weights
            cache=(2*s['layers']*s['kv_heads']*s['head_dim']*s['kv_bytes']*s['context'])/10**9
            need=1.2*weight+cache
        else: weight=cache=None;need=s['required']
        estimate=need;vendor_gb=None
        if s.get('vendor_setting')==(s.get('context'),s.get('batch')):
            peaks=[s['vendor_bf16'][0]] if s['bits']==16 else [s['vendor_gptq'][0],s['vendor_awq'][0]]
            vendor_gb=max(peaks)*(2**30)/(10**9)
            need=max(need,vendor_gb)
        candidates={'single':[],'multi':[],'memory_only':[]}
        for p,r,v in entries:
            supported=_support(r,s);n=r['gpu_count']
            if v>=need: candidates['single' if supported else 'memory_only'].append((p,r,v))
            if v<need and s['kind']=='estimate' and supported and n in {2,4,8} and s['heads']%n==s['kv_heads']%n==0 and v*n>=need:
                candidates['multi'].append((p,r,v))
        rows={};heads={};selected=[]
        for section,choices in candidates.items():
            identities={(r['gpu_model'],r['price_type'],r['gpu_count'] if section=='multi' else 0) for _,r,_ in choices}
            minima=[]
            for identity in identities:
                local=[e for e in choices if (e[1]['gpu_model'],e[1]['price_type'],e[1]['gpu_count'] if section=='multi' else 0)==identity]
                minima.append(sorted(local,key=lambda e:_order(e,section=='multi'))[0])
            minima.sort(key=lambda e:_order(e,section=='multi'))
            rows[section]=[dict(path=p,offer=r,vram=v,headroom=(v*r['gpu_count'] if section=='multi' else v)/need) for p,r,v in minima]
            if section!='memory_only': selected.extend((p,r) for p,r,v in minima)
            heads[section]={}
            for kind in ('on_demand','spot'):
                local=sorted(((p,r) for p,r,v in choices if r['price_type']==kind),key=lambda e:_order(e,True))
                heads[section][kind]=local[0] if local else None
        cards={p:r for p,r in selected}
        cards.update((x['path'],x['offer']) for x in rows['memory_only'])
        cards.update(e for group in ('single','multi') for e in heads[group].values() if e)
        good={p:r for group in ('single','multi') for p,r,v in candidates[group]}
        answer[slug]=dict(required=need,estimate=estimate,vendor_gb=vendor_gb,weights=weight,kv=cache,
            unquantized_parameters=unquantized,unquantized_weights=unquantized_weights,quantized_weights=quantized_weights,parameters=s.get('parameters'),bytes=s.get('bits',0)/8,
            context=s.get('context'),rows=rows,heads=heads,models=sorted({r['gpu_model'] for p,r in selected}),
            selected=selected,cards=sorted(cards.items(),key=lambda e:_order(e,True)),
            count=len({(r['gpu_model'],r['price_type']) for p,r in selected}),offers=len(good),
            updated=max((r['collected_at'] for r in good.values()),default=None),siblings=[],unreviewed=sorted(unreviewed))
        answer[slug]['overall']={kind:heads['single'][kind] or heads['multi'][kind] for kind in ('on_demand','spot')}
    # Independently find the strictest page with this primary answer and floor.
    for slug,d in answer.items():
        primary='single' if d['rows']['single'] else 'multi'
        identity={(x['offer']['gpu_model'],x['offer']['price_type']) for x in d['rows'][primary]}
        peers=[]
        for other,q in answer.items():
            other_primary='single' if q['rows']['single'] else 'multi'
            if WORKLOADS[other]['kind']!=WORKLOADS[slug]['kind'] or other_primary!=primary: continue
            if identity=={(x['offer']['gpu_model'],x['offer']['price_type']) for x in q['rows'][primary]}: peers.append(other)
        target=sorted(peers,key=lambda key:(answer[key]['required'],key))[-1]
        d['siblings']=[] if slug==target else [dict(slug=target,sections=[primary])]
        d['indexable']=d['count']>=3 and slug==target
    return answer


def audit_entry(page,raw,tr,filename,errors):
    from .audience_audit import Tags
    sections=re.findall(r'<section data-workload-entry="true">(.*?)</section>',raw,re.S)
    if len(sections)!=1:
        errors.append(filename+': missing workload entry point');return
    expected=[tr.language.path(route(s)) for members in WORKLOAD_FAMILIES.values() for s in members]
    if [a.get('href') for tag,a in Tags(sections[0]).tags if tag=='a']!=expected:
        errors.append(filename+': workload entry links mismatch')
    wanted=tr.text('workload_entry_title')+tr.text('workload_entry_intro')+''.join(tr.text('workload_family_'+family)+''.join(tr.text('workload_'+WORKLOADS[s]['key']+'_title') for s in members) for family,members in WORKLOAD_FAMILIES.items())
    if unescape(re.sub('<[^>]+>','',sections[0]))!=wanted: errors.append(filename+': workload entry labels mismatch')
    if [a.get('data-workload-family') for tag,a in Tags(sections[0]).tags if 'data-workload-family' in a]!=list(WORKLOAD_FAMILIES):
        errors.append(filename+': workload family grouping mismatch')


def audit_page(page,raw,slug,data,facts,tr,origin,slugs,filename,errors):
    from .audit import _audit_cards,_allowed_literals,_dollars,_resolve
    from .audience_audit import Tags
    spec=WORKLOADS[slug];d=data[slug];base='/workloads/'+slug;values=[]
    title=tr.text('workload_'+spec['key']+'_title')
    choices=[e for e in (*d['overall'].values(),*d['heads']['multi'].values()) if e]
    price=_dollars(min(_bill(e[1]) for e in choices)) if choices else tr.text('workload_meta_no_price')
    if choices: price=tr.text('workload_meta_price',price=price)
    description=tr.text('workload_description',workload=' '.join(filter(None,(spec['model'],'FP16' if spec.get('bits')==16 else tr.text('workload_precision_4bit') if spec.get('bits')==4 else ''))),
        memory=format(d['required'],'.3f').rstrip('0').rstrip('.'),models=len(d['models']),price=price,date=(d['updated'] or facts['date'])[:10])
    if tr.language.code not in ('zh-Hans','ja','ko') and len(description)>155: errors.append(filename+': workload description exceeds 155 characters')
    expected={}
    def text(key,**kw): expected['workload:'+key]=tr.text('workload_'+key,**kw)
    def metric(field):
        fmt='text' if field=='unquantized_parameters' or field=='parameters' and 'parameter_count' in spec else 'decimal3' if field in ('kv','required','estimate','vendor_gb','unquantized_weights','quantized_weights') else 'number'
        v=str(d[field]) if fmt=='text' else format(d[field],'.3f').rstrip('0').rstrip('.') if fmt=='decimal3' else format(d[field],'g')
        values.append((base+'/'+field,fmt,v));return v
    text('intro',model=spec['model']);text('basis');text('requirement')
    if spec['kind']=='estimate':
        # Same field occurs twice in the formula.
        parameters=metric('parameters');weight=metric('weights');metric('weights');cache=metric('kv');context=metric('context')
        if spec['bits']==4:
            text('weight_rule_4bit',parameters=parameters,vocab=spec['vocab'],hidden=spec['hidden'],
                copies=1 if spec['tie_word_embeddings'] else 2,tied=str(spec['tie_word_embeddings']).lower(),
                unquantized=metric('unquantized_parameters'),unquantized_weights=metric('unquantized_weights'),
                quantized_weights=metric('quantized_weights'),weights=weight,kv=cache,context=context,memory=metric('estimate'))
            text('weight_inputs',sources='; '.join(tr.text(SOURCES[s][0]) for s in spec['weight_input_sources']))
            blocks=re.findall(r'<p data-search="workload:weight_inputs">(.*?)</p>',raw,re.S)
            if len(blocks)!=1 or [a.get('href') for tag,a in Tags(blocks[0]).tags if tag=='a']!=[SOURCES[s][1] for s in spec['weight_input_sources']]:
                errors.append(filename+': weight input links mismatch')
            if slug.startswith('llama-'): text('rounded_parameters')
            for fmt,source,size in spec.get('checkpoint_sizes',()):
                key='checkpoint_'+fmt.lower()
                text(key,source=tr.text(SOURCES[source][0]),size=size,memory=f'{size/1e9:.6f}')
                blocks=re.findall(r'<p data-search="workload:'+key+r'">(.*?)</p>',raw,re.S)
                if len(blocks)!=1 or [a.get('href') for tag,a in Tags(blocks[0]).tags if tag=='a']!=[SOURCES[source][1]]:
                    errors.append(filename+': quantized weight index link mismatch')
        else:
            text('weight_rule',parameters=parameters,bytes=metric('bytes'),weights=weight,kv=cache,memory=metric('required'),context=context)
        if 'parameter_count' in spec:
            text('exact_inputs',parameters=spec['parameter_count'],hidden=spec['hidden'],intermediate=spec['intermediate'],
                vocab=spec['vocab'],context=spec['context'],maximum=spec['max_context'])
        # Actual order follows the translated formula; pointer multiplicities
        # are checked below, and every value is resolved independently in SQL.
        text('weight_caveat')
        text('kv_formula',layers=spec['layers'],heads=spec['kv_heads'],dim=spec['head_dim'],context=spec['context'],kv=format(d['kv'],'.3f').rstrip('0').rstrip('.'))
        if spec['kv_per_1k'] is not None: text('kv_blog')
        if spec['parameters']==13: text('kv_llama2')
        text('architecture',heads=spec['heads'],kv_heads=spec['kv_heads'])
        if spec.get('same_as'):
            text('same_estimate',models='; '.join(SAME_ARCHITECTURE[s].split('/')[-1] for s in spec['same_as']))
            alias_blocks=re.findall(r'<p data-search="workload:same_estimate">(.*?)</p>',raw,re.S)
            if len(alias_blocks)!=1 or [a.get('href') for tag,a in Tags(alias_blocks[0]).tags if tag=='a']!=[SOURCES[s][1] for s in spec['same_as']]:
                errors.append(filename+': same-architecture config links mismatch')
        if 'vendor_bf16' in spec:
            text('vendor_context',developer='Qwen',source=tr.text('workload_source_qwen_benchmark'),code=tr.text('workload_source_qwen_benchmark_code'))
            vendor_blocks=re.findall(r'<p data-search="workload:vendor_context">(.*?)</p>',raw,re.S)
            if len(vendor_blocks)!=1 or [a.get('href') for tag,a in Tags(vendor_blocks[0]).tags if tag=='a']!=[SOURCES[s][1] for s in ('qwen_benchmark','qwen_benchmark_code')]:
                errors.append(filename+': vendor benchmark link mismatch')
            if spec['bits']==16:
                memory,n=spec['vendor_bf16'];text('vendor_bf16',developer='Qwen',memory=f'{memory:.2f}',gpus=n)
            else:
                memory,n=spec['vendor_gptq'];awq,awq_n=spec['vendor_awq']
                text('vendor_4bit',developer='Qwen',memory=f'{memory:.2f}',gpus=n,awq=f'{awq:.2f}',awq_gpus=awq_n)
            if d['vendor_gb'] is not None:
                text('vendor_screen',estimate=metric('estimate'),vendor=metric('vendor_gb'),memory=metric('required'))
                if n*80<d['required']: text('vendor_smaller',developer='Qwen',gpus=n)
        if spec.get('vendor_overview'):
            source=SOURCES[spec['vendor_overview']]
            text('vendor_mistral',developer='Mistral AI',source=tr.text(source[0]))
            blocks=re.findall(r'<p data-search="workload:vendor_mistral">(.*?)</p>',raw,re.S)
            if len(blocks)!=1 or [a.get('href') for tag,a in Tags(blocks[0]).tags if tag=='a']!=[source[1]]:
                errors.append(filename+': Mistral vendor overview link mismatch')
        if spec['bits']==4: text('quant_caveat')
        text('floor_llm')
    else: text('whisper_rule',memory=metric('required'));text('floor_whisper')
    text('screening' if spec['kind']=='estimate' else 'screening_whisper')
    if d['updated']: values.append((base+'/updated','date',d['updated'][:10]))
    text('counts',offers=metric('offers'),minima=metric('count'))
    anchors={p:'#quote-'+str(i) for i,(p,r) in enumerate(d['cards'])};best_links=[]
    text('overall')
    for g in ('overall','single','multi') if spec['kind']=='estimate' else ('overall','single'):
        if g!='overall': text(g)
        for kind in ('on_demand','spot'):
            entry=d['overall'][kind] if g=='overall' else d['heads'][g][kind];copy=tr.text(kind)+tr.label_separator
            if g=='multi' and not entry and d['heads']['single'][kind]:
                copy+=tr.text('workload_multi_not_needed',memory=format(d['required'],'.3f').rstrip('0').rstrip('.'))
            elif entry:
                p,r=entry;amount=_dollars(_bill(r));n=format(r['gpu_count'],'g')
                copy+=tr.text('workload_best_line_'+('one' if r['gpu_count']==1 else 'other'),model=r['gpu_model'],provider=PROVIDERS[r['provider']]['name'],count=n,price=amount)
                if g in ('single','overall') and entry is d['heads']['single'][kind] and r['gpu_count']>1: copy+=' · '+tr.text('workload_single_bundle',n=n)
                values.extend(((p+'/gpu_count','number',n),(p,'instance-money',amount)))
                best_links.append(anchors[p])
            else: copy+=tr.text('not_observed')
            expected['workload:best:'+g+':'+kind]=copy
    blocks=re.findall(r'<p data-search="workload:best:[^"]+">(.*?)</p>',raw,re.S)
    if [a.get('href') for tag,a in Tags(''.join(blocks)).tags if tag=='a']!=best_links: errors.append(filename+': workload headline links mismatch')
    text('minima_note' if spec['kind']=='estimate' else 'minima_note_whisper')
    if d['unreviewed']: expected['workload:unreviewed-models']=' · '.join(d['unreviewed'])
    disclosures=re.findall(r'<details data-workload-unreviewed="true"><summary>(.*?)</summary>',raw,re.S)
    if disclosures!=([tr.html('workload_not_reviewed')] if d['unreviewed'] else []): errors.append(filename+': unreviewed disclosure mismatch')
    expected_rows=[];table_links=[];permitted=_allowed_literals(tr,facts)|{title}
    for group in ('single','multi','memory_only'):
        if group=='multi' and spec['kind']!='estimate': continue
        text(group+'_table')
        if group=='multi': text('tp_rule')
        for i,x in enumerate(d['rows'][group]):
            p,r=x['path'],x['offer'];name=r['gpu_model'];gpu=gpu_software(name)
            badges=[]
            if _arm(r): badges.append(tr.text('workload_arm'))
            if gpu and gpu['vendor']!='NVIDIA': badges.append(tr.text('workload_non_cuda'))
            if not _support(r,spec): badges.append(tr.text('workload_below' if gpu else 'workload_unreviewed'))
            target=('CUDA CC '+format(gpu['cc'],'.1f') if gpu['cc'] is not None else gpu['gfx']) if gpu else tr.text('workload_unreviewed')
            hardware=gpu['vendor']+' · '+gpu['architecture']+' · '+target if gpu else target
            permitted.add(hardware)
            if group=='multi':
                badges.append(tr.text('workload_tp_row',n=format(r['gpu_count'],'g')))
                if model_identity(name) in PCIE_ONLY: badges.append(tr.text('workload_pcie'))
            if group=='single' and r['gpu_count']>1: badges.append(tr.text('workload_single_bundle',n=format(r['gpu_count'],'g')))
            permitted.add(' · '.join(badges));permitted.add('· '+' · '.join(badges))
            row=[name,format(x['vram'],'g'),tr.text(r['price_type']),_dollars(r['price_per_gpu_hour']),format(r['gpu_count'],'g'),_dollars(_bill(r)),tr.text('workload_tight' if x['headroom']<1.25 else 'workload_comfortable'),' · '.join([hardware]+badges)]
            expected_rows.append(row)
            table_links.extend((tr.language.path('/'+slugs[name]+'/'),capacity_spec(name)['source'],anchors[p]))
            if gpu: table_links.append(gpu['source'])
            values.extend(((base+'/rows/'+group+'/'+str(i)+'/vram','number',row[1]),(p+'/price_per_gpu_hour','money',row[3]),(p+'/gpu_count','number',row[4]),(p,'instance-money',row[5])))
        if not d['rows'][group]:
            if group=='multi' and d['rows']['single']: text('multi_not_needed',memory=format(d['required'],'.3f').rstrip('0').rstrip('.'))
            else: text('empty_'+group)
    if page.table_rows!=expected_rows: errors.append(filename+': workload rows, capacity, headroom, software or ranking mismatch')
    tables=re.findall(r'<table>(.*?)</table>',raw,re.S)
    if len(tables)!=(3 if spec['kind']=='estimate' else 2) or [a.get('href') for tag,a in Tags(''.join(tables)).tags if tag=='a']!=table_links:
        errors.append(filename+': workload table/source/quote links mismatch')
    columns=[tr.text(k) for k in ('gpu_model','workload_vram','price_type','workload_rate','workload_gpus','workload_instance_price','workload_headroom','workload_support')]
    plain=lambda tag:[unescape(re.sub('<[^>]+>','',x)) for x in re.findall('<'+tag+r'\b[^>]*>(.*?)</'+tag+'>',raw,re.S)]
    if plain('th')!=columns*len(tables) or plain('caption')!=[tr.text('workload_units')]*len(tables): errors.append(filename+': workload table header/units mismatch')
    labels=[a.get('aria-label') for tag,a in Tags(raw).tags if tag=='div' and a.get('class')=='table-wrap']
    if labels!=[tr.text('workload_units')]*len(tables): errors.append(filename+': workload accessible units mismatch')
    for sibling in d['siblings']:
        other=sibling['slug'];expected['workload:sibling:'+other]=tr.text('workload_sibling',sibling=tr.text('workload_'+WORKLOADS[other]['key']+'_title'),sections=' / '.join(tr.text('workload_'+g+'_table') for g in sibling['sections']))
        if tr.language.path(route(other)) not in [a.get('href') for tag,a in page.links if tag=='a']: errors.append(filename+': missing sibling link')
    if d['count']<3: text('thin')
    for key in ('check_title','check_memory','check_software','check_cpu','check_host')+(('check_interconnect',) if spec['kind']=='estimate' else ())+('check_cost','check_stock','sources','related'): text(key)
    expected['workload:source-list']=''.join(tr.text(SOURCES[s][0])+' · '+SOURCES[s][1] for s in spec['sources'])
    if page.search_values!=expected: errors.append(filename+': workload estimate, floor, headline, sibling or prose mismatch')
    from collections import Counter
    if Counter(v for v in page.outside_values if v[0] not in ('/page_updated','/built'))!=Counter(values): errors.append(filename+': workload arithmetic/count/table pointers mismatch')
    _audit_cards(page,'workload',tr,facts,filename,errors,entries_override=d['cards'],order_override=lambda e:_order(e,True))
    sections=re.findall(r'<section id="(quote-[^"]+)">(.*?)</section>',raw,re.S)
    if [key for key,_ in sections]!=['quote-'+str(i) for i in range(len(d['cards']))]: errors.append(filename+': workload quote anchors mismatch')
    for (_,body),(p,r) in zip(sections,d['cards']):
        if [a.get('data-offer') for tag,a in Tags(body).tags if 'data-offer' in a]!=[p]: errors.append(filename+': quote anchor points at wrong offer')
    urls=[SOURCES[s][1] for s in spec['sources']]
    sources=re.findall(r'<ul data-search="workload:source-list">(.*?)</ul>',raw,re.S)
    if len(sources)!=1 or [a.get('href') for tag,a in Tags(sources[0]).tags if tag=='a']!=urls: errors.append(filename+': workload official source URLs mismatch')
    y,w,_=date.fromisoformat(facts['date']).isocalendar()
    links={a.get('href') for tag,a in page.links if tag=='a'}
    required_links={tr.language.path('/guides/'+s+'/') for s in GUIDE_LINKS}|{tr.language.path('/reports/'),tr.language.path(f'/reports/{y}-W{w:02d}/')}
    if not required_links<=links: errors.append(filename+': workload guide/report link missing')
    url=origin+tr.language.path(route(slug))
    article={'@context':'https://schema.org','@type':'Article','headline':title,'description':description,
        'inLanguage':tr.language.code,'url':url,'mainEntityOfPage':url,**({'dateModified':d['updated']} if d['updated'] else {}),'citation':urls}
    if [s for s in page.structured if s.get('@type')=='Article']!=[article]: errors.append(filename+': workload Article mismatch')
    if page.h1!=[title] or page.title!=tr.text('page_title',title=title,site_name=tr.text('site_name')): errors.append(filename+': workload title mismatch')
    for field,value in {'description':description,'og:description':description,'twitter:description':description,'og:title':title,'twitter:title':title}.items():
        if page.metadata.get(field)!=value: errors.append(filename+': workload metadata mismatch '+field)
    if (page.metadata.get('robots')!='noindex')!=d['indexable']: errors.append(filename+': workload thin/duplicate noindex mismatch')
    for p,fmt,value in page.values:
        try:
            v=_resolve({**facts,'workloads':data,'page_updated':d['updated']},p)
            wanted=(v[:10] if fmt=='date' else datetime.fromisoformat(v.replace('Z','+00:00')).strftime('%H:%M UTC') if fmt=='clock' else _dollars(v) if fmt=='money' else format(v,'.3f').rstrip('0').rstrip('.') if fmt=='decimal3' else format(v,'g') if fmt=='number' else _dollars(_bill(v)) if fmt.startswith('instance-') else _dollars(_bill(v)*v['minimum_billing_seconds']/3600) if fmt.startswith('minimum-charge-') else str(v))
            if value!=wanted: errors.append(filename+': workload statistic differs from SQL '+p)
        except (KeyError,IndexError,TypeError,ValueError): errors.append(filename+': invalid workload statistic '+p)
    footer=[('/built','date',facts['built'][:10])]+([('/page_updated','date',d['updated'][:10])] if d['updated'] else [])
    if any(v not in page.values for v in footer): errors.append(filename+': workload footer dates missing')
    permitted.update(urls)
    permitted.update(d['unreviewed'])
    permitted.add(' · '.join(d['unreviewed']))
    if any(t not in permitted for t in page.unmarked): errors.append(filename+': unaudited workload numeric text')
    tags=Tags(raw).tags
    if any('hidden' in a or 'style' in a or a.get('aria-hidden')=='true' or any(k.lower().startswith('on') for k in a) for tag,a in tags): errors.append(filename+': workload content hidden or executable')
    if any(tag=='script' and (a.get('type')!='application/ld+json' or 'src' in a) for tag,a in tags) or page.metadata.get('refresh'): errors.append(filename+': executable workload page')
    css=Path(__file__).with_name('design.css').read_text(encoding='utf-8')
    css+='\n[data-growth="embed"] textarea{display:block;width:100%;max-width:100%;box-sizing:border-box;resize:vertical;font:13px/1.5 ui-monospace,monospace;padding:10px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px}\n'
    if [n[2] for n in page.growth_nodes if n[0]=='style']!=[css] or any(tag=='link' and a.get('rel')=='stylesheet' for tag,a in tags): errors.append(filename+': unaudited workload stylesheet')
    visible=unescape(re.sub('<[^>]+>',' ',re.sub(r'<(script|style)\b[^>]*>.*?</\1>',' ',raw,flags=re.S|re.I)))
    # The cited AMD product name contains AI; only that exact official name is
    # allowed, rather than a blanket exception to the site's wording rule.
    visible=visible.replace('Ryzen AI MAX / AI 300','Ryzen')
    visible=visible.replace('Mistral AI','Mistral')
    if re.search(r'\bAI\b',visible): errors.append(filename+': forbidden workload wording')
    if raw.count('<article data-workload="'+slug+'">')!=1: errors.append(filename+': visible workload article missing')
    return expected,title,description
