"""Guardia de gela-data (repo PÚBLICO): nada privado de GELA Tracker puede entrar.

Revisa lo que va a entrar en un commit (o en un push) y lo frena si:
  1. Un archivo está fuera de las carpetas permitidas (dane/, drafts/, .github/, .githooks/, tools/, README.md, .gitignore).
  2. El nombre parece privado (data.json, copias, .env, bases de datos, claves).
  3. El contenido trae claves o tokens (Anthropic, GitHub, Google, AWS, Slack, llaves privadas).
  4. El contenido trae estructuras de GELA Tracker ("legalAnalyzer", "timeEntries", "linkedin": …).
  5. En tu Mac: nombres de clientes, correos y dominios que salen de tu propio data.json
     (se leen en el momento, nunca se guardan en el repo).
  6. Un archivo pesa más de 25 MB.

Uso:
  python tools/guard.py --staged          (pre-commit y el bot antes de commitear)
  python tools/guard.py --range A..B      (pre-push: todo lo que vas a subir)
  python tools/guard.py --all             (revisa el repo entero)
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOWED = re.compile(r'^(dane/|drafts/|\.github/|\.githooks/|tools/|README\.md$|\.gitignore$)')
BAD_NAME = re.compile(r'(^|/)(data(\.backup[^/]*)?\.json|.*\.backup.*|\.env.*|.*\.(sqlite3?|db|pem|key|p12|pfx|keychain)|id_rsa.*|credentials.*|secrets?\..*|token.*)$', re.I)
SECRETS = [
    (r'sk-ant-[A-Za-z0-9_-]{10,}', 'clave de Anthropic'),
    (r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}', 'token de GitHub'),
    (r'AIza[0-9A-Za-z_-]{30,}', 'clave de Google'),
    (r'ya29\.[0-9A-Za-z_-]{20,}', 'token OAuth de Google'),
    (r'AKIA[0-9A-Z]{16}', 'clave de AWS'),
    (r'xox[abposr]-[0-9A-Za-z-]{10,}', 'token de Slack'),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', 'llave privada'),
    (r'eyJhbGciOi[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}', 'JWT (Supabase u otro)'),
    (r'AQ[A-Za-z0-9_-]{100,}', 'token de LinkedIn'),
]
TRACKER = [
    r'"legalAnalyzer"\s*:', r'"timeEntries"\s*:', r'"linkedin"\s*:\s*\{', r'"clients"\s*:\s*\[', r'"tasks"\s*:\s*\[\s*\{',
    r'"radar"\s*:\s*\{', r'"corridors"\s*:\s*\[', r'"prospects"\s*:\s*\[', r'"studio"\s*:\s*\{', r'"invoices"\s*:\s*\[',
]
PRIVATE_DOMAINS = ['gartneradvisors.de', 'gartneradvisors.uk', 'gmail.com', 'caribou-esg', 'strata-advisors']
GENERIC = {'personal', 'búsqueda de empleo', 'busqueda de empleo', 'gärtner', 'gartner'}
DATA_JSON = Path.home() / 'Documents' / 'GELA Tracker' / 'data.json'
MAX_BYTES = 25 * 1024 * 1024
SAFE_EMAIL = re.compile(r'actions@users\.noreply\.github\.com|noreply@anthropic\.com')


def git(*a):
    return subprocess.run(['git', *a], capture_output=True, check=True).stdout


def local_words():
    """Nombres y correos de TU data.json (solo existe en tu Mac; en GitHub Actions no hay)."""
    if not DATA_JSON.exists():
        return []
    try:
        d = json.loads(DATA_JSON.read_text())
    except Exception:  # noqa: BLE001
        return []
    out = set()
    for key in ('clients', 'prospects', 'contacts', 'deals'):
        for c in d.get(key) or []:
            if not isinstance(c, dict):
                continue
            for f in ('name', 'company', 'email', 'contact', 'domain', 'website'):
                v = str(c.get(f) or '').strip()
                if len(v) >= 5 and v.lower() not in GENERIC:
                    out.add(v)
                    # También sin la forma societaria: "Ejemplo Energía S.A.S." -> "Ejemplo Energía"
                    short = re.sub(r'[\s,]+(s\.?\s?a\.?\s?s\.?|s\.?\s?a\.?|gmbh|ug|ag|ltd\.?|llc|inc\.?|sl|s\.?l\.?)$', '', v, flags=re.I).strip()
                    if len(short) >= 5 and short.lower() not in GENERIC:
                        out.add(short)
    # Correos que aparezcan en cualquier parte de data.json
    out.update(m for m in re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', DATA_JSON.read_text()) if not SAFE_EMAIL.search(m))
    return sorted(out, key=len, reverse=True)[:5000]


def check(files, read):
    words = local_words()
    word_re = [re.compile(r'(?<![\w@.])' + re.escape(w) + r'(?![\w])') for w in words]
    probs = []
    for f in files:
        if not ALLOWED.search(f):
            probs.append(f'{f}: fuera de las carpetas permitidas')
            continue
        if BAD_NAME.search(f):
            probs.append(f'{f}: nombre de archivo privado')
            continue
        data = read(f)
        if data is None:
            continue
        if len(data) > MAX_BYTES:
            probs.append(f'{f}: pesa {len(data) // 1048576} MB (máx. 25)')
            continue
        txt = data.decode('utf-8', 'ignore')
        for pat, what in SECRETS:
            if re.search(pat, txt):
                probs.append(f'{f}: {what}')
        for pat in TRACKER:
            if re.search(pat, txt):
                probs.append(f'{f}: estructura de GELA Tracker ({pat.split(chr(34))[1]})')
        for dom in PRIVATE_DOMAINS:
            if re.search(r'[\w.+-]+@' + re.escape(dom), txt, re.I):
                probs.append(f'{f}: correo de {dom}')
        for w, rx in zip(words, word_re):
            if rx.search(txt):
                probs.append(f'{f}: aparece "{w}" (está en tu data.json)')
    return probs


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else '--staged'
    if mode == '--staged':
        files = git('diff', '--cached', '--name-only', '--diff-filter=ACMR').decode().split('\n')
        read = lambda f: git('show', f':{f}')  # noqa: E731
    elif mode == '--range':
        rng = sys.argv[2]
        files = git('diff', '--name-only', '--diff-filter=ACMR', rng).decode().split('\n')
        tip = rng.split('..')[-1]
        read = lambda f: git('show', f'{tip}:{f}')  # noqa: E731
    else:
        files = git('ls-files').decode().split('\n')
        read = lambda f: Path(f).read_bytes() if Path(f).exists() else None  # noqa: E731
    files = [f for f in files if f]
    probs = check(files, read)
    if probs:
        print('\n🛑 gela-data es PÚBLICO y esto parece privado. No se hizo el commit/push:\n')
        for p in probs[:50]:
            print('  - ' + p)
        print('\nSaca esos archivos (git restore --staged <archivo>) y vuelve a intentar.')
        print('Si de verdad es público y es un falso positivo, dile a Claude que ajuste tools/guard.py.')
        sys.exit(1)
    print(f'gela-data guard: {len(files)} archivo(s) revisados, nada privado.')


if __name__ == '__main__':
    main()
