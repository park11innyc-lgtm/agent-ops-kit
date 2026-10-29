#!/usr/bin/env python3
"""Memory-store secret redactor (Stop hook): scrub known secret values out of an agent's long-term memory store.

Memory plugins such as claude-mem store raw tool input/output in a SQLite DB with no path-based exclusion, so
any tool call that touched a credential lands there in plaintext. This hook collects the secret VALUES from the
stores listed in settings (`mem_redact.secret_stores`: *.md notes, .env files, *.json token files, a bare token
file, or `claude.json:<mcp-server>` env blocks) and replaces them with [REDACTED] in the DB and its text logs.
Never prints a value. Gated: it only runs when the memory store or a secret store changed since the last clean pass.

Usage: mem_redact_v1.py [--dry-run] [--list]      settings: settings_v1.json next to this file, or MEM_REDACT_SETTINGS
"""
import json, os, re, sqlite3, sys, time

X = os.path.expanduser
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.environ.get('MEM_REDACT_SETTINGS') or os.path.join(HERE, 'settings_v1.json')))['mem_redact']
ROOT = X(CFG['mem_root'])
DB = os.path.join(ROOT, CFG.get('db_name', 'claude-mem.db'))
MARK = '[REDACTED]'
TEXT_EXT = ('.log', '.jsonl', '.txt', '.md')
SKIP_FILES = {'settings.json', '.env', 'telemetry.json', 'supervisor.json', 'worker.pid'}
STATE = X(CFG['state_file'])
MIN_KEYED = int(CFG.get('min_keyed_secret_len', 8))  # floor for values under a secret key; shorter values would over-redact common text

NON_SECRET_LINE = re.compile(CFG.get('non_secret_line_pattern') or r'(?!x)x', re.I)
SECRET_LABEL = re.compile(r'(password|\bpw\b|token|secret)', re.I)
SECRET_KEY = re.compile(r'(token|secret|key|pass|pwd|webhook|auth|cookie|session|private|refresh|access|dsn|uri|url)', re.I)
ENV_NAME = re.compile(r'^[A-Z][A-Z0-9]*_[A-Z0-9_]+$')  # shaped like an env-var NAME; never applied to a value under a secret key
FILE_NAME = re.compile(r'\.(py|gs|js|json|md|txt|csv|db|sql)$')
HOST = re.compile(r'^[a-z0-9\-.]+\.(com|net|io|co|app|dev)$')
URI_PW = re.compile(r'://[^:\s/@]+:([^@\s]+)@')


def stores():
    return list(CFG.get('secret_stores', []))


def keep(tok, secret_key=False):
    tok = tok.strip().strip('"\'')
    if len(tok) < (MIN_KEYED if secret_key else 12) or (tok.isdigit() and not secret_key) or HOST.match(re.split(r'[:/]', tok)[0]) or FILE_NAME.search(tok):
        return None
    if not secret_key and ENV_NAME.match(tok):
        return None
    if tok.lower() in ('true', 'false', 'null', 'none', 'changeme', 'your-token-here'):
        return None
    return tok


def from_md(path, vals):
    for line in open(path, encoding='utf-8', errors='replace').read().splitlines():
        vals.update(URI_PW.findall(line))
        if NON_SECRET_LINE.search(line.strip()):
            continue
        labelled = bool(SECRET_LABEL.search(line))
        for tok in re.findall(r'[A-Za-z0-9_\-+/=.~:]{20,}', line):
            tok = tok.strip('.:')
            if tok.isdigit() or HOST.match(re.split(r'[:/]', tok)[0]) or FILE_NAME.search(tok) or (not labelled and ENV_NAME.match(tok)) or tok.startswith(('postgres', 'user=')):
                continue
            if tok.startswith('/') and not tok.startswith('//'):
                continue
            mixed = re.search(r'\d', tok) and re.search(r'[A-Za-z]', tok)
            if labelled or mixed or tok.startswith('//'):
                vals.add(tok)


def from_kv(key, value, vals):
    if not isinstance(value, str):
        return
    vals.update(URI_PW.findall(value))
    if value.startswith('http') and '@' not in value and not re.search(r'(token|secret|webhook|key)', key, re.I):
        return
    secret_key = bool(SECRET_KEY.search(key))
    if secret_key or (re.search(r'\d', value) and re.search(r'[A-Za-z]', value) and len(value) >= 24):
        t = keep(value, secret_key)
        if t:
            vals.add(t)


def from_env(path, vals):
    for line in open(path, encoding='utf-8', errors='replace'):
        m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if m:
            from_kv(m.group(1), m.group(2).split(' #')[0].strip().strip('"\''), vals)


def from_json(obj, vals, key=''):
    if isinstance(obj, dict):
        for k, v in obj.items():
            from_json(v, vals, k)
    elif isinstance(obj, list):
        for v in obj:
            from_json(v, vals, key)
    else:
        from_kv(key, obj, vals)


def secret_values(listing=None):
    vals = set()
    for s in stores():
        before = len(vals)
        try:
            if s.startswith('claude.json:'):
                srv = json.load(open(X('~/.claude.json'))).get('mcpServers', {}).get(s.split(':', 1)[1], {})
                from_json(srv.get('env', {}), vals)
                for a in srv.get('args', []):
                    vals.update(URI_PW.findall(a))
            elif not os.path.exists(X(s)):
                continue
            elif s.endswith('.md'):
                from_md(X(s), vals)
            elif s.endswith('.json'):
                from_json(json.load(open(X(s))), vals)
            elif s.endswith('token'):
                # bare single-value token file (e.g. .board-token): the whole file is the secret
                vals.add(open(X(s), encoding='utf-8', errors='replace').read().strip())
            else:
                from_env(X(s), vals)
        except Exception:
            continue
        if listing is not None:
            listing.append((s, len(vals) - before))
    return sorted((v for v in vals if len(v) >= MIN_KEYED), key=len, reverse=True)


def source_mtimes():
    out = []
    for s in stores():
        p = X('~/.claude.json') if s.startswith('claude.json:') else X(s)
        if os.path.exists(p):
            out.append(os.path.getmtime(p))
    return out


def gate(dry):
    newest = max((os.path.getmtime(os.path.join(dp, f)) for dp, _, fs in os.walk(ROOT) for f in fs), default=0)
    newest = max([newest] + source_mtimes())
    try:
        last = json.load(open(STATE)).get('clean_at', 0)
    except Exception:
        last = 0
    return dry or newest > last


def q(name):
    """quote an SQL identifier read from the DB itself (a table named with a double quote must not break out)"""
    return '"' + name.replace('"', '""') + '"'


def scrub_db(vals, dry):
    hits = 0
    con = sqlite3.connect(DB, timeout=15)
    con.execute('PRAGMA secure_delete=ON')
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like '%\\_fts%' escape '\\' and name not like 'sqlite_%'")]
    for t in tables:
        cols = [c[1] for c in con.execute(f'pragma table_info({q(t)})')]  # every column; the row filter below keeps text cells only
        for c in cols:
            for v in vals:
                n = con.execute(f'select count(*) from {q(t)} where typeof({q(c)}) = \'text\' and instr({q(c)}, ?) > 0', (v,)).fetchone()[0]
                if n:
                    hits += n
                    if not dry:
                        con.execute(f'update {q(t)} set {q(c)} = replace({q(c)}, ?, ?) where typeof({q(c)}) = \'text\' and instr({q(c)}, ?) > 0', (v, MARK, v))
    if not dry:
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    con.close()
    return hits


def mask_in_place(fp, vals):
    """overwrite each value with same-length asterisks at its byte offset; never truncates or rewrites the file,
    so records a live writer appends meanwhile land after the masked bytes and are kept"""
    with open(fp, 'r+b') as fh:
        raw = fh.read()
        for v in vals:
            b = v.encode()
            i = raw.find(b)
            while i >= 0:
                fh.seek(i)
                fh.write(b'*' * len(b))
                i = raw.find(b, i + len(b))
        fh.flush()
        os.fsync(fh.fileno())


def scrub_files(vals, dry):
    hits, flagged = 0, []
    for dp, _, fs in os.walk(ROOT):
        for f in fs:
            fp = os.path.join(dp, f)
            if f in SKIP_FILES or f.startswith('claude-mem.db'):
                continue
            try:
                raw = open(fp, 'rb').read()
            except Exception:
                continue
            n = sum(raw.count(v.encode()) for v in vals)
            if not n:
                continue
            hits += n
            if f.endswith(TEXT_EXT) and not dry:
                mask_in_place(fp, vals)
            elif not f.endswith(TEXT_EXT):
                flagged.append(os.path.relpath(fp, ROOT))
    return hits, flagged


def main():
    dry = '--dry-run' in sys.argv or '--list' in sys.argv
    if '--list' in sys.argv:
        L = []
        v = secret_values(L)
        print(f'{len(v)} secret values from {len(L)} stores (settings)')
        for s, n in L:
            print(f'  {n:>3} new  {s}')
    if not os.path.exists(DB) or not gate(dry):
        return
    started = time.time()  # watermark: anything written after this is re-checked next run
    vals = secret_values()
    db_hits = scrub_db(vals, dry)
    file_hits, flagged = scrub_files(vals, dry)
    if not dry and not flagged:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump({'clean_at': started, 'values_checked': len(vals), 'version': 1}, open(STATE, 'w'))
    if db_hits or file_hits or dry:
        verb = 'found' if dry else 'redacted'
        msg = f'[mem-redact v1] {verb} {db_hits} DB cells, {file_hits} file hits; {len(vals)} values checked'
        if flagged:
            msg += f'; binary files still holding secrets: {", ".join(flagged)}'
        print(msg, file=sys.stderr)


if __name__ == '__main__':
    main()
