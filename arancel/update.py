"""Arancel de Aduanas de Colombia vigente (Decreto 1881 de 2021, compilado por la DIAN).

La consulta del arancel en MUISCA es interactiva y no se puede descargar. La misma
norma, compilada y mantenida por la DIAN, está en su normograma como una sola página
(unos 5,5 MB) con las ~8.000 subpartidas nacionales a 10 dígitos:

    código | designación | gravamen (%) <NVn><NVm>

Las marcas <NVn> enlazan al decreto que modificó esa subpartida (p. ej. el 414 de 2021
para confecciones). Su detalle (fecha, título, desde cuándo rige) está en el "Resumen
de Notas de Vigencia" de la misma página. Este programa lo junta todo:

    arancel/<HS2>.json   lo que lee la app: subpartidas del capítulo, partidas (HS4) y
                         los decretos que tocan ese capítulo
    arancel/meta.json    controles: cuántas líneas, reparto de tarifas, último decreto

Ojo: la columna de gravamen es la del texto compilado. Cuando una subpartida tiene
modificaciones temporales, la DIAN las anota con <NV> y la tarifa temporal puede no
estar reflejada en la columna; por eso cada línea lleva sus decretos, y la app lo
contrasta con la tarifa que de verdad se cobra (microdatos de importación del DANE).
La propia página avisa que las notas del Decreto 1530 de 2024 están "en proceso de
incorporación".

Uso: python arancel/update.py
"""
import html as H
import json, os, re, sys
from collections import defaultdict
from datetime import datetime, timezone

import requests

URL = 'https://normograma.dian.gov.co/dian/compilacion/docs/decreto_1881_2021.htm'
BASE = 'https://normograma.dian.gov.co/dian/compilacion/docs/'
# Las notas de vigencia no están en el HTML: las arma este script de la página.
NOTES_JS = BASE + 'js/decreto_1881_2021.js'
HERE = os.path.dirname(os.path.abspath(__file__))
UA = {'User-Agent': 'Mozilla/5.0 (gela-data; +https://github.com/gartneradvisors/gela-data)'}
MIN_LINES = 7000
LOG = []


def log(*a):
    line = ' '.join(str(x) for x in a)
    LOG.append(line); print(line, flush=True)


def clean(s):
    s = re.sub(r'<br\s*/?>', ' ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    return re.sub(r'\s+', ' ', H.unescape(s)).strip()


def mods_summary(js):
    """Notas de vigencia: las funciones insRowN() del script de la página llenan
    description[n] con párrafos "- Modificado por el Decreto …"."""
    strings = re.findall(r'description\[\d+\]\s*=\s*"((?:[^"\\]|\\.)*)"', js, re.S)
    text = ' '.join(strings).replace('\\"', '"')
    out = {}
    for p in re.findall(r'<p>(.*?)</p>', text, re.S | re.I):
        plain = clean(p)
        # Decretos que la nota cita por enlace (p. ej. una lista "…414 del 16 de abril de 2021, 423…")
        for f, n_, y_ in re.findall(r"href=['\"](decreto_(\d{1,4})_(\d{4})\.htm)", p, re.I):
            out.setdefault(f'{int(n_):04d}_{y_}', {'decree': f'Decreto {int(n_)} de {y_}', 'date': None, 'title': None, 'effective': None,
                                                   'url': BASE + f.lower(), 'text': plain[:600], 'mentioned': True})
        if not re.search(r'modificad|adicionad|suspendid|prorrogad|sustituid', plain, re.I):
            continue
        dm = re.search(r'Decreto\s+(?:n[úu]mero\s+)?(\d{1,4})\s+(?:de|del)\s+((?:\d{1,2}\s+de\s+\w+\s+de\s+)?(\d{4}))', plain, re.I)
        if not dm:
            continue
        n, date, year = int(dm.group(1)), dm.group(2), dm.group(3)
        key = f'{n:04d}_{year}'
        title = re.search(r"['‘\"“](.+?)['’\"”]\s*,\s*publicad", plain)
        rige = re.search(r'(Rige[^.]*\.|Entra a regir[^.]*\.|Estar[áa] vigente[^.]*\.)', plain)
        if key in out and out[key].get('mentioned'):
            del out[key]
        out.setdefault(key, {
            'decree': f'Decreto {n} de {year}', 'date': date,
            'title': title.group(1).strip() if title else None,
            'effective': rige.group(1).strip() if rige else None,
            'url': BASE + f'decreto_{n:04d}_{year}.htm',
            'text': plain[:600],
        })
    return out


def parse(page):
    lines, headings, refs = {}, {}, defaultdict(set)
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S | re.I):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S | re.I)
        if not cells:
            continue
        code = clean(cells[0])
        if re.fullmatch(r'\d{2}\.\d{2}', code) and len(cells) >= 2:
            headings[code.replace('.', '')] = clean(cells[1])
            continue
        if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d{2}', code) or len(cells) < 3:
            continue
        rate_cell = cells[-1]
        rm = re.match(r'\s*(\d+(?:[.,]\d+)?)', clean(rate_cell))
        if not rm:
            continue
        rate = float(rm.group(1).replace(',', '.'))
        mods = []
        for href in re.findall(r'href=["\'](decreto_(\d{1,4})_(\d{4})\.htm)', rate_cell, re.I):
            k = f'{int(href[1]):04d}_{href[2]}'
            if k not in mods:
                mods.append(k)
        c10 = code.replace('.', '')
        lines[c10] = [clean(cells[1]), int(rate) if rate.is_integer() else rate, mods]
        for k in mods:
            refs[k].add(c10[:2])
    return lines, headings, refs


def main():
    r = requests.get(URL, headers=UA, timeout=180)
    r.raise_for_status()
    page = r.content.decode('cp1252', 'replace')
    log(f'Página: {len(page)} caracteres')
    lines, headings, refs = parse(page)
    try:
        j = requests.get(NOTES_JS, headers=UA, timeout=60)
        mods = mods_summary(j.content.decode('cp1252', 'replace')) if j.ok else {}
        if not j.ok:
            log(f'Notas de vigencia: HTTP {j.status_code}')
    except Exception as e:  # noqa: BLE001
        log('Notas de vigencia: no se pudieron leer', repr(e)); mods = {}
    log(f'Subpartidas: {len(lines)} · partidas: {len(headings)} · decretos en el resumen: {len(mods)} · decretos citados en líneas: {len(refs)}')
    if len(lines) < MIN_LINES:
        log(f'Menos de {MIN_LINES} subpartidas: la página cambió de formato o vino incompleta. No se toca lo publicado.')
        sys.exit(1)
    asof = datetime.now(timezone.utc).isoformat(timespec='seconds')
    by_ch = defaultdict(dict)
    for c, v in lines.items():
        by_ch[c[:2]][c] = v
    note = ('Gravamen del Arancel de Aduanas (Decreto 1881 de 2021) según el texto compilado por la DIAN. '
            'Las subpartidas con decretos asociados tienen modificaciones, a veces temporales, que pueden no estar '
            'reflejadas en la columna: revisa el decreto. La DIAN indica que las notas del Decreto 1530 de 2024 '
            'están en proceso de incorporación.')
    for ch, ls in by_ch.items():
        used = sorted({k for v in ls.values() for k in v[2]})
        with open(os.path.join(HERE, f'{ch}.json'), 'w', encoding='utf-8') as fh:
            json.dump({'asOf': asof, 'source': URL, 'note': note, 'fields': ['designacion', 'gravamen', 'decretos'],
                       'lines': ls, 'headings': {k: v for k, v in headings.items() if k[:2] == ch},
                       'decrees': {k: mods.get(k) or {'decree': f'Decreto {int(k[:4])} de {k[5:]}', 'url': f'{BASE}decreto_{k}.htm'} for k in used}},
                      fh, ensure_ascii=False, separators=(',', ':'))
    dist = defaultdict(int)
    for v in lines.values():
        dist[str(v[1])] += 1
    year_of = lambda m: int((re.search(r'(\d{4})$', m.get('date') or '') or re.search(r'(\d{4})$', m['decree'])).group(1))
    latest = sorted([m for m in mods.values() if not m.get('mentioned')], key=year_of, reverse=True)[:8]
    with open(os.path.join(HERE, 'meta.json'), 'w', encoding='utf-8') as fh:
        json.dump({'asOf': asof, 'source': URL, 'lines': len(lines), 'headings': len(headings), 'chapters': len(by_ch),
                   'withDecrees': sum(1 for v in lines.values() if v[2]),
                   'rates': dict(sorted(dist.items(), key=lambda x: -x[1])), 'decreesInSummary': len(mods),
                   'latestDecrees': [{k: m[k] for k in ('decree', 'date', 'title', 'effective')} for m in latest],
                   'examples': {k: lines.get(k) for k in ('0901111000', '6109100000', '8703231090', '3004902900')}},
                  fh, ensure_ascii=False, indent=1)
    log('Listo.')


if __name__ == '__main__':
    try:
        main()
    finally:
        with open(os.path.join(HERE, 'last_run.log'), 'w', encoding='utf-8') as fh:
            fh.write(datetime.now(timezone.utc).isoformat(timespec='seconds') + '\n' + '\n'.join(LOG) + '\n')
