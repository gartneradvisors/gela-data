"""Proyectos normativos de Colombia en consulta pública (2026-10-10).

Lee cada día las páginas de proyectos de los ministerios (decretos,
resoluciones y circulares publicados para comentarios) y deja en
drafts/co.json, para GELA Tracker:

  título, entidad, tipo, enlace, fecha de publicación, inicio y cierre de
  comentarios.

Corre en GitHub Actions (EE. UU.) porque varias de estas páginas no abren desde
Europa. Usa un Chromium real (Playwright) porque algunas cargan la lista con
JavaScript (DIAN) o con desafíos anti-bots.

Uso: python drafts/update.py [--only minambiente,mincit-dec] [--no-browser]
"""
import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'co.json'
STATE = ROOT / 'state.json'
DEBUG = ROOT / 'debug'
LOG = []
TODAY = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-5))).date()  # hora de Bogotá
Y = TODAY.year


def log(*a):
    s = ' '.join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


# ---------------------------------------------------------------- fuentes
# mode: rss (feed de WordPress) | html (página con la lista)
# detail: entrar a la ficha de cada proyecto cuando la lista no trae fechas
# link: regex que debe cumplir el enlace del proyecto (sobre la URL absoluta)
SOURCES = [
    dict(id='minambiente', entity='MinAmbiente', mode='rss', url='https://www.minambiente.gov.co/consulta/feed/',
         detail=True, link=r'minambiente\.gov\.co/consulta/'),
    dict(id='mininterior', entity='MinInterior', mode='html', url='https://www.mininterior.gov.co/proyectos-de-decreto/',
         kind='decreto', detail=True, link=r'mininterior\.gov\.co/(?!proyectos-de-decreto/?$)[a-z0-9-]*(proyecto|pd-|por-)'),
    dict(id='minhacienda', entity='MinHacienda', mode='html', url='https://www.minhacienda.gov.co/normativa/proyectos-de-decretos',
         kind='decreto', detail=False, link=r'/documents/|/document_library/'),
    dict(id='mincit-dec', entity='MinCIT', mode='html', url=f'https://www.mincit.gov.co/normatividad/proyectos-de-normatividad/proyectos-de-decreto-{Y}',
         kind='decreto', detail=False, link=r'proyectos-de-decreto-\d{4}/.+\.aspx'),
    dict(id='mincit-res', entity='MinCIT', mode='html', url=f'https://www.mincit.gov.co/normatividad/proyectos-de-normatividad/proyectos-de-resolucion-{Y}',
         kind='resolución', detail=False, link=r'proyectos-de-resolucion-\d{4}/.+\.aspx'),
    dict(id='mincit-cir', entity='MinCIT', mode='html', url=f'https://www.mincit.gov.co/normatividad/proyectos-de-normatividad/proyectos-de-circular-{Y}',
         kind='circular', detail=False, link=r'proyectos-de-circular-\d{4}/.+\.aspx'),
    dict(id='minenergia', entity='MinEnergía', mode='html', url='https://minenergia.gov.co/es/servicio-al-ciudadano/foros/',
         detail=True, link=r'/servicio-al-ciudadano/foros/[^/?#]+'),
    dict(id='dian', entity='DIAN', mode='html', url='https://www.dian.gov.co/normatividad/Paginas/ProyectosNormas.aspx',
         kind='resolución', detail=False, link=r'dian\.gov\.co/.+\.(pdf|aspx)', js_wait=6000),
    dict(id='dnp', entity='DNP', mode='html', url='https://www.dnp.gov.co/normativa/proyectos-de-normatividad',
         detail=True, link=r'dnp\.gov\.co/'),
]

# Enlaces que son anexos del proyecto y no el proyecto.
SKIP = re.compile(r'memoria|informe|observaci|respuesta|matriz|estudio t[eé]cnico|formato|anexo|comentarios recibidos|\bio[-_]|[-/]mj[-_]|^inf_|soporte|cronograma|encuesta', re.I)
DRAFT = re.compile(r'proyecto|borrador|por (el|la|medio de (el|la)) cual|decreto|resoluci[oó]n|circular', re.I)
NOISE = re.compile(r'esquema de publicaci|agenda regulatoria|manual de funciones|planta de personal|plan anual|rendici[oó]n de cuentas', re.I)

# ---------------------------------------------------------------- fechas
MESES = {m: i + 1 for i, m in enumerate(['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'])}
MESES['setiembre'] = 9
ABBR = {k[:3]: v for k, v in MESES.items()}
MES = r'(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)'
D = r'(\d{1,2})(?:°|º|o)?'
YR = r'(?:\s+de(?:l)?\s+(?:año\s+)?(\d{4}))?'


def mk(y, m, d):
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def num_date(s):
    """dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd."""
    m = re.match(r'\s*(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if m:
        return mk(m[1], m[2], m[3])
    m = re.match(r'\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})', s)
    if m:
        return mk(m[3], m[2], m[1])
    return None


def word_date(s, year=None):
    """'9 de octubre de 2026', 'octubre 08, 2026', '9 oct 2026'."""
    m = re.match(r'\s*' + D + r'\s+de\s+' + MES + YR, s, re.I)
    if m:
        return mk(m[3] or year, MESES[m[2].lower()], m[1])
    m = re.match(r'\s*' + MES + r'\s+(\d{1,2}),?\s+(\d{4})', s, re.I)
    if m:
        return mk(m[3], MESES[m[1].lower()], m[2])
    m = re.match(r'\s*(\d{1,2})\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\.?\s+(\d{4})', s, re.I)
    if m:
        return mk(m[3], ABBR[m[2].lower()], m[1])
    return None


def any_date(s, year=None):
    return num_date(s) or word_date(s, year)


ANYD = r'(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{1,2}(?:°|º|o)?\s+de\s+' + MES + YR + r'|' + MES + r'\s+\d{1,2},?\s+\d{4})'


def windows(text):
    """Devuelve (publicado, desde, hasta) con lo que se pueda leer del texto."""
    t = re.sub(r'\s+', ' ', text or '')
    pub = frm = until = None
    years = [int(y) for y in re.findall(r'\b(20\d\d)\b', t)]
    year = max(years) if years else Y
    untils = []

    # Etiquetas: "Desde: 09-10-2026 Hasta: 24-10-2026", "Fecha inicio: … Fecha fin: …"
    m = re.search(r'(?:desde|fecha\s+(?:de\s+)?inicio|inicio)\s*:?\s*' + ANYD, t, re.I)
    if m:
        frm = any_date(m.group(1), year)
    for m in re.finditer(r'(?:hasta|fecha\s+(?:de\s+)?(?:fin(?:alizaci[oó]n)?|cierre)|cierre|fin)\s*:?\s*' + ANYD, t, re.I):
        d = any_date(m.group(1), year)
        if d:
            untils.append(d)

    # "desde el 3 (de octubre)? (de 2026)? y hasta el 7 de octubre de 2026"; "del 8 al 18 de octubre de 2026"
    rng = re.compile(r'(?:desde\s+(?:el\s+)?(?:d[ií]a\s+)?|del\s+(?:d[ií]a\s+)?)' + D + r'(?:\s+de\s+' + MES + r')?' + YR +
                     r',?\s+(?:y\s+)?(?:hasta|al)\s+(?:el\s+)?(?:d[ií]a\s+)?' + D + r'\s+de\s+' + MES + YR, re.I)
    for m in rng.finditer(t):
        y2 = int(m[6] or m[3] or year)
        mo2 = MESES[m[5].lower()]
        a = mk(m[3] or y2, MESES[m[2].lower()] if m[2] else mo2, m[1])
        b = mk(y2, mo2, m[4])
        if a and b and a > b:  # "del 28 de diciembre al 5 de enero de 2027"
            a = mk(y2 - 1, a.month, a.day)
        if b:
            untils.append(b)
            if a and not frm:
                frm = a
    # "hasta el 25 de mayo de 2026" (ampliaciones incluidas)
    for m in re.finditer(r'hasta\s+(?:el\s+)?(?:d[ií]a\s+)?(?:las\s+[^,.;]{0,25}?\s+del?\s+)?' + D + r'\s+de\s+' + MES + YR, t, re.I):
        d = mk(m[3] or year, MESES[m[2].lower()], m[1])
        if d:
            untils.append(d)
    # Publicación
    m = re.search(r'publicad[oa][^.]{0,120}?desde\s+(?:el\s+)?' + D + r'\s+de\s+' + MES + YR, t, re.I)
    if m:
        pub = mk(m[3] or year, MESES[m[2].lower()], m[1])
    if not pub:
        m = re.search(r'(?:fecha\s+de\s+publicaci[oó]n|publicado(?:\s+el)?)\s*:?\s*' + ANYD, t, re.I)
        if m:
            pub = any_date(m.group(1), year)
    if not pub:
        m = re.match(r'\s*' + MES + r'\s+(\d{1,2}),?\s+(\d{4})', t, re.I)  # encabezado de fecha (MinHacienda)
        if m:
            pub = mk(m[3], MESES[m[1].lower()], m[2])

    lo = (frm or pub or TODAY) - timedelta(days=5)
    hi = (frm or pub or TODAY) + timedelta(days=150)
    untils = [u for u in untils if lo <= u <= hi and (not frm or u >= frm)]
    if untils:
        until = max(untils)  # una ampliación siempre mueve el cierre hacia adelante
    if not frm and pub and until and pub <= until:
        frm = pub
    return pub, frm, until


def kind_of(title, url, default=None):
    s = f'{title} {url}'.lower()
    if re.search(r'circular', s):
        return 'circular'
    if re.search(r'por (el|medio del) cual|decreto|\bpd[-_ +]', s) and not re.search(r'por (la|medio de la) cual', s):
        return 'decreto'
    if re.search(r'por (la|medio de la) cual|resoluci', s):
        return 'resolución'
    if re.search(r'\bley\b|proyecto de ley', s):
        return 'ley'
    return default or 'otro'


def clean(s, n=400):
    s = re.sub(r'\s+', ' ', s or '').strip(' -–·:')
    return s[:n]


def title_from(anchor_text, ctx):
    m = re.search(r'["“]?((?:proyecto de (?:decreto|resoluci[oó]n|circular)[^.]{0,40}?)?por (?:el|la|medio de(?:l)? (?:el|la)?|medio del) cual[^"”]{10,500}?)(?:["”]|\.\s|$| Plazo| Desde| Fecha)', ctx, re.I)
    a = clean(anchor_text, 400)
    if m and len(m.group(1)) > len(a) - 5:
        return clean(m.group(1), 400)
    if len(a) >= 25 and not re.match(r'^(descargar|ver|aqu[ií]|documento|pdf|link|enlace|m[aá]s)', a, re.I):
        return a
    return clean(ctx, 300)


# ---------------------------------------------------------------- red
class Fetcher:
    def __init__(self, browser=True):
        self.pw = self.br = self.ctx = None
        if browser:
            try:
                from playwright.sync_api import sync_playwright
                self.pw = sync_playwright().start()
                self.br = self.pw.chromium.launch(args=['--disable-blink-features=AutomationControlled'])
                self.ctx = self.br.new_context(locale='es-CO', user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
            except Exception as e:  # noqa: BLE001
                log('  sin navegador:', e)
        import requests
        self.s = requests.Session()
        self.s.headers['User-Agent'] = 'Mozilla/5.0 (GELA Tracker; +https://github.com/gartneradvisors/gela-data)'

    def raw(self, url):
        r = self.s.get(url, timeout=45)
        r.raise_for_status()
        return r.text

    def page(self, url, wait=2500):
        if not self.ctx:
            return self.raw(url)
        p = self.ctx.new_page()
        try:
            p.goto(url, wait_until='domcontentloaded', timeout=60000)
            try:
                p.wait_for_load_state('networkidle', timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            p.wait_for_timeout(wait)
            return p.content()
        finally:
            p.close()

    def close(self):
        if self.br:
            self.br.close()
        if self.pw:
            self.pw.stop()


# ---------------------------------------------------------------- listas
def soup_of(html):
    s = BeautifulSoup(html, 'lxml')
    for t in s(['script', 'style', 'noscript', 'svg', 'nav', 'header', 'footer', 'form']):
        t.decompose()
    return s


def container_of(a):
    best = None
    for i, p in enumerate(a.parents):
        if i > 7 or p.name in ('body', 'html', 'main'):
            break
        if p.name in ('tr', 'li', 'article'):
            return p
        n = len(p.get_text(' ', strip=True))
        if p.name in ('div', 'section', 'p', 'td') and 60 <= n <= 2500 and best is None:
            best = p
    return best or a.parent


def context_of(box):
    txt = box.get_text(' ', strip=True)
    if re.search(ANYD, txt, re.I):
        return txt
    # Fechas fuera de la caja (encabezados de fecha, párrafo anterior al enlace)
    prev = []
    for sib in box.find_previous_siblings(limit=3):
        prev.insert(0, sib.get_text(' ', strip=True))
    return clean(' '.join(prev + [txt]), 3000)


def parse_list(html, src):
    s = soup_of(html)
    link_re = re.compile(src['link'], re.I) if src.get('link') else None
    host = urlparse(src['url']).netloc.replace('www.', '')
    out, boxes = [], set()
    for a in s.find_all('a', href=True):
        href = urljoin(src['url'], a['href'].strip())
        if not href.startswith('http') or href.split('#')[0].rstrip('/') == src['url'].rstrip('/'):
            continue
        text = clean(a.get_text(' ', strip=True), 600)
        if link_re and not link_re.search(href):
            continue
        if not link_re and host not in href:
            continue
        name = urlparse(href).path.rsplit('/', 1)[-1]
        if SKIP.search(text) or SKIP.search(name):
            continue
        box = container_of(a)
        if id(box) in boxes:
            continue
        ctx = clean(box.get_text(' ', strip=True), 3000)
        if not (DRAFT.search(text) or DRAFT.search(ctx) or DRAFT.search(href)):
            continue
        boxes.add(id(box))
        full = context_of(box)
        title = title_from(text, ctx)
        if NOISE.search(title) or len(title) < 20:
            continue
        out.append(dict(url=href, title=title, ctx=full))
    return out


def parse_rss(xml):
    s = BeautifulSoup(xml, 'xml')
    out = []
    for it in s.find_all('item'):
        title = clean(it.title.get_text() if it.title else '', 500)
        link = clean(it.link.get_text() if it.link else '', 500)
        pub = None
        if it.pubDate:
            try:
                pub = datetime.strptime(it.pubDate.get_text().strip()[:25], '%a, %d %b %Y %H:%M:%S').date()
            except ValueError:
                pass
        if title and link and not NOISE.search(title):
            out.append(dict(url=link, title=title, ctx='', pub=pub.isoformat() if pub else None))
    return out


def detail_text(html):
    s = soup_of(html)
    main = s.find('main') or s.find('article') or s.find(class_=re.compile('content|entry|post', re.I)) or s.body or s
    txt = main.get_text(' ', strip=True)
    docs = []
    for a in main.find_all('a', href=True):
        h = a['href']
        if re.search(r'\.(pdf|zip|docx?)(\?|$)', h, re.I) or re.search(r'descargar', a.get_text(), re.I):
            docs.append(h)
    return clean(txt, 6000), docs[:5]


# ---------------------------------------------------------------- pasada
def key(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def run(only=None, browser=True, detail_cap=40):
    state = json.loads(STATE.read_text()) if STATE.exists() else {'items': {}, 'baseline': {}}
    items, baseline = state['items'], state['baseline']
    DEBUG.mkdir(exist_ok=True)
    F = Fetcher(browser)
    health = []
    budget = detail_cap
    try:
        for src in SOURCES:
            if only and src['id'] not in only:
                continue
            log(f"== {src['id']}: {src['url']}")
            try:
                if src['mode'] == 'rss':
                    body = F.raw(src['url'])
                    found = parse_rss(body)
                else:
                    body = F.page(src['url'], src.get('js_wait', 2500))
                    found = parse_list(body, src)
                (DEBUG / f"{src['id']}.html").write_text(body[:400000])
            except Exception as e:  # noqa: BLE001
                log('   ERROR', e)
                health.append(dict(id=src['id'], entity=src['entity'], url=src['url'], ok=False, n=0, error=str(e)[:300]))
                continue
            first = not baseline.get(src['id'])
            now_iso = TODAY.isoformat()
            n_new = 0
            for f in found:
                k = key(f['url'])
                cur = items.get(k)
                if not cur:
                    cur = items[k] = dict(id=k, src=src['id'], entity=src['entity'], url=f['url'], firstSeen=now_iso, baseline=first)
                    n_new += 0 if first else 1
                cur['title'] = f['title']
                cur['lastSeen'] = now_iso
                pub, frm, until = windows(f['ctx'])
                if f.get('pub') and not pub:
                    pub = date.fromisoformat(f['pub'])
                for k2, v in (('published', pub), ('from', frm), ('until', until)):
                    if v and (k2 != 'until' or not cur.get('until') or v.isoformat() > cur['until']):
                        cur[k2] = v.isoformat()
                cur['kind'] = kind_of(cur['title'], cur['url'], src.get('kind'))
                # Ficha: si la lista no dice el cierre, o si el proyecto sigue abierto (pueden ampliar el plazo).
                open_now = cur.get('until') and cur['until'] >= now_iso
                fresh = (cur.get('published') or cur['firstSeen']) >= (TODAY - timedelta(days=45)).isoformat()
                need = src.get('detail') and fresh and cur.get('checked') != now_iso and (not cur.get('until') or open_now)
                if need and budget > 0 and not re.search(r'\.(pdf|zip|docx?)(\?|$)', cur['url'], re.I):
                    budget -= 1
                    try:
                        txt, docs = detail_text(F.page(cur['url'], 1500))
                        p2, f2, u2 = windows(txt)
                        if p2 and not cur.get('published'):
                            cur['published'] = p2.isoformat()
                        if f2 and not cur.get('from'):
                            cur['from'] = f2.isoformat()
                        if u2 and (not cur.get('until') or u2.isoformat() > cur['until']):
                            cur['until'] = u2.isoformat()
                        if docs:
                            cur['docs'] = [urljoin(cur['url'], d) for d in docs]
                        if not cur.get('summary'):
                            cur['summary'] = clean(txt, 600)
                        cur['checked'] = now_iso
                    except Exception as e:  # noqa: BLE001
                        log('   ficha', cur['url'], e)
                time.sleep(0.3)
            baseline[src['id']] = True
            dated = sum(1 for f in found if items[key(f['url'])].get('until'))
            log(f"   {len(found)} proyectos en la página, {dated} con cierre de comentarios, {n_new} nuevos")
            health.append(dict(id=src['id'], entity=src['entity'], url=src['url'], ok=len(found) > 0, n=len(found), dated=dated,
                               error=None if found else 'La página no trajo proyectos (¿cambió el formato?)'))
    finally:
        F.close()

    # Lo que lee la app: abiertos, recién cerrados (60 días) y nuevos sin fecha.
    keep_from = (TODAY - timedelta(days=60)).isoformat()
    new_from = (TODAY - timedelta(days=30)).isoformat()
    out = []
    for it in items.values():
        u = it.get('until')
        if u and u >= keep_from:
            out.append(it)
        elif not u and not it.get('baseline') and it['firstSeen'] >= new_from:
            out.append(it)
        elif not u and it.get('published', '') >= new_from:
            out.append(it)
    out.sort(key=lambda x: (x.get('until') or '9999', x.get('published') or ''), reverse=True)
    pub_keys = ['id', 'src', 'entity', 'kind', 'title', 'url', 'docs', 'published', 'from', 'until', 'firstSeen', 'summary']
    OUT.write_text(json.dumps(dict(
        asOf=datetime.now(timezone.utc).isoformat(timespec='seconds'), today=TODAY.isoformat(),
        sources=health, items=[{k: it[k] for k in pub_keys if it.get(k)} for it in out],
    ), ensure_ascii=False, indent=1))
    # Estado: se olvida lo que lleva más de 400 días sin verse.
    old = (TODAY - timedelta(days=400)).isoformat()
    state['items'] = {k: v for k, v in items.items() if v.get('lastSeen', '') >= old}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0))
    log(f"Listo: {len(out)} proyectos en co.json ({sum(1 for x in out if (x.get('until') or '') >= TODAY.isoformat())} abiertos)")
    (ROOT / 'last_run.log').write_text('\n'.join(LOG))
    return out, health


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    _, h = run(set(filter(None, a.only.split(','))) or None, browser=not a.no_browser)
    sys.exit(0 if any(x['ok'] for x in h) else 1)
