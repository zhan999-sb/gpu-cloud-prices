"""Public IndexNow ownership key and deterministic build-time page hashes."""
import hashlib
import re

# Public by design: deployed verbatim at /<KEY>.txt, never a credential.
KEY = 'd6ef1384259a4c708385f2af990ac9a1'


def page_manifest(pages, routes, origin):
    hashes = {}
    for route in sorted(routes):
        filename = route.lstrip('/')
        filename = filename+'index.html' if not filename or filename.endswith('/') else filename if filename.endswith('.html') else filename+'.html'
        # A build-clock tick alone is not a changed page. Observation times,
        # prices, qualifiers, navigation, structured data and styles all count.
        content = re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>',
                         '<time data-stat="/built"></time>', pages[filename])
        hashes[origin+route] = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return {'version':1, 'origin':origin, 'pages':hashes}
