"""Build-time search content; no HTTP, third-party imports or synthetic prices."""
from datetime import date, timedelta
from html import escape
import re
from statistics import median
import struct
import zlib

from .schema import Offer
from .regions import eligible
from .providers.registry import PROVIDERS
from .search_specs import PAIRS, REFERENCES, SPECS

OFFER_PAGE_SIZE = 12


def summary(data):
    good = [o for o in data['offers'] if eligible(Offer(**o))]
    values = [o['price_per_gpu_hour'] for o in good]
    day = date.fromisoformat(data['observation_day'])
    trends = {}
    for kind, series in data['history'].items():
        points = [p for p in series if day - timedelta(days=29) <= date.fromisoformat(p['day']) <= day]
        trends[kind] = (dict(first=points[0]['day'], last=points[-1]['day'],
            change=(points[-1]['min']/points[0]['min']-1)*100, days=len(points))
            if len(points) >= 2 else None)
    return dict(min=min(values) if values else None, median=median(values) if values else None,
                offers=len(good), providers=len({o['provider'] for o in good}),
                on_demand=sum(o['price_type']=='on_demand' for o in good),
                spot=sum(o['price_type']=='spot' for o in good), trends=trends,
                updated=max((o['collected_at'] for o in good), default=None))


def title(model, data, tr):
    if model == 'RTX 4090 24GB':
        return tr.text('seo_4090_title')
    if re.fullmatch(r'(H100|A100)(?: .*GB)?', model):
        return tr.text('seo_h100_title' if model.startswith('H100') else 'seo_a100_title', model=model)
    s = summary(data)
    return tr.text('search_title' if s['updated'] else 'search_empty_title', model=model,
                   date=(s['updated'] or data['last_updated'] or data['observation_day'])[:10])


def buyer_key(model):
    if model == 'RTX 4090 24GB':
        return 'seo_4090_intro'
    if re.fullmatch(r'(H100|A100)(?: .*GB)?', model):
        return 'seo_h100_intro' if model.startswith('H100') else 'seo_a100_intro'
    return None


def intro(data, tr):
    s = summary(data)
    if not s['offers']:
        return tr.text('search_empty_intro')
    money = lambda v: f'${v:.2f}' if v >= 1 else f'${v:.3f}'
    return tr.text('search_intro', minimum=money(s['min']), median=money(s['median']),
                   offers=s['offers'], providers=s['providers'], demand=s['on_demand'], spot=s['spot'])


def trend_text(data, tr):
    parts = []
    for kind, t in summary(data)['trends'].items():
        if t:
            parts.append(tr.text('search_trend', kind=tr.text(kind), first=t['first'], last=t['last'],
                                 change=f"{t['change']:+.2f}%", days=t['days']))
    return ' '.join(parts) if parts else tr.text('search_trend_wait')


def provider_slug(key):
    return 'clore-ai' if key == 'clore' else key.replace('_', '-')


def family_models(models, family):
    # Explicit desktop/form-factor whitelist; laptop, D and modded capacities
    # cannot silently become the consumer reference configuration.
    patterns = {'H100': r'H100 (?:SXM 80GB|PCIe 80GB|NVL 94GB)',
                'A100': r'A100(?: (?:SXM4|SXM|PCIe))? (?:40|80)GB',
                'H200': r'H200(?: SXM5)? 141GB', 'L40S': r'L40S 48GB',
                'RTX 4090': r'RTX 4090 24GB', 'RTX 5090': r'RTX 5090 32GB'}
    return sorted(m for m in models if re.fullmatch(patterns[family], m))


def entries(models):
    return [('/models/'+m.replace('~','~0').replace('/','~1')+'/offers/'+str(i), o)
            for m,d in models.items() for i,o in enumerate(d['offers'])]


def ordered(entries):
    return sorted(entries, key=lambda p: (p[1]['currency']!='USD' or p[1]['price_scope']=='gpu_component'
        or p[1]['price_per_gpu_hour'] is None, p[1]['currency'],
        p[1]['price_per_gpu_hour'] if p[1]['price_per_gpu_hour'] is not None else p[1]['price_per_hour'], p[0]))


def model_chunk(data, model, page=0):
    local = ordered(entries({model: data}))
    return local[page*OFFER_PAGE_SIZE:(page+1)*OFFER_PAGE_SIZE]


def breadcrumb(items, tr, origin, route):
    html = '<nav class="breadcrumbs" aria-label="'+tr.html('breadcrumbs')+'">'
    html += ' / '.join('<a href="'+tr.language.path(path)+'">'+escape(label)+'</a>' for label,path in items)
    html += '</nav>'
    ld = {'@context':'https://schema.org', '@type':'BreadcrumbList', 'inLanguage':tr.language.code,
          'url':origin+tr.language.path(route), 'itemListElement':[
              {'@type':'ListItem','position':i+1,'name':label,'item':origin+tr.language.path(path)}
              for i,(label,path) in enumerate(items)]}
    return html, ld


def related(model, data, models, slugs, tr):
    body = '<nav class="related"><h2>'+tr.html('search_related')+'</h2><ul>'
    for key in sorted({o['provider'] for o in data['offers']}):
        body += '<li><a href="'+tr.language.path('/providers/'+provider_slug(key)+'/')+'">'+escape(PROVIDERS[key]['name'])+'</a></li>'
    for slug, pair in PAIRS.items():
        if any(model in family_models(models, family) for family in pair):
            body += '<li><a href="'+tr.language.path('/compare/'+slug+'/')+'">'+tr.html('compare_label', left=pair[0],right=pair[1])+'</a></li>'
    return body+'</ul></nav>'


def pagination(model, count, slug, tr):
    if count <= OFFER_PAGE_SIZE:
        return ''
    return '<nav class="pagination"><p>'+tr.html('offer_pages_note')+'</p>'+ ' · '.join(
        '<a href="'+tr.language.path('/'+slug+'/' if i==0 else '/'+slug+'/offers/'+str(i+1)+'/')+'">'
        +tr.html('offer_page',model=model,number=i+1)+'</a>' for i in range((count+OFFER_PAGE_SIZE-1)//OFFER_PAGE_SIZE))+'</nav>'


# Tiny original 5x7 bitmap alphabet. Rasterized directly as RGB PNG, with zlib
# and CRC from stdlib. No SVG converter, OS font or CI package is required.
from .bitmap_font import GLYPHS as _GLYPHS


def social_png(model, data):
    """Language-neutral model identity and observed USD units, 1200x630."""
    width,height=1200,630
    pixels=bytearray(bytes((16,34,62))*width*height)
    def draw(text,x,y,scale,color):
        for c in text.upper():
            glyph=_GLYPHS.get(c,_GLYPHS['?'])
            for i,bit in enumerate(glyph):
                if bit=='1':
                    for dy in range(scale):
                        yy=y+(i//5)*scale+dy
                        xx=x+(i%5)*scale
                        if 0<=yy<height and 0<=xx<width-scale:
                            pos=(yy*width+xx)*3
                            pixels[pos:pos+scale*3]=bytes(color)*scale
            x+=6*scale
    draw('GPU PRICE INDEX',60,55,6,(131,181,255))
    # Long raw model names wrap instead of overflowing the graphic.
    lines=[model[i:i+30] for i in range(0,min(len(model),90),30)]
    for i,line in enumerate(lines):draw(line,60,145+i*65,5,(255,255,255))
    # Keep price-type, marketplace/fee and tax qualifications with the actual
    # localized quotes. A shared image carries identity/date/units only.
    s=summary(data)
    unit=data['currency']+' / GPU-HR' if data['currency']!='MULTI' else 'GPU-HR'
    draw(unit,60,365,5,(131,181,255))
    stamp=(s['updated'] or data['last_updated'] or data['observation_day'])[:10]
    draw(stamp,60,450,4,(220,230,245))
    def chunk(kind,payload):
        return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload))
    scan=b''.join(b'\0'+pixels[y*width*3:(y+1)*width*3] for y in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(scan,9))+chunk(b'IEND',b'')
