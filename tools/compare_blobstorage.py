"""Compare blobs.csv (from analyze.py) with the real blobstorage listing.

Listing (run inside the blobstorage dir):
    find . -name '*.blob' -printf '%P\\t%s\\n' > blob_sizes.tsv

Usage: python compare_blobstorage.py <blobs.csv> <blob_sizes.tsv> <outdir>
Writes <outdir>/blobstorage.md, missing_blobs.csv, extra_blobs.csv.
"""
import csv
import os
import sys
from collections import Counter, defaultdict


def human(n):
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.1f} PB'


def oid_of(path):
    return int(''.join(p[2:] for p in path.split('/')[:-1]), 16)


def main(blobs_csv, sizes_tsv, outdir):
    real = {}
    with open(sizes_tsv) as f:
        for line in f:
            path, size = line.rstrip('\n').split('\t')
            real[path] = int(size)

    rows = {}
    with open(blobs_csv, newline='') as f:
        for r in csv.DictReader(f):
            rows[r['blob_path']] = r
    cur_oids = {int(r['oid'], 16) for r in rows.values()}

    agg = defaultdict(lambda: [0, 0, 0, 0, 0])  # n, declared, real, n_missing, n_zero
    missing, bad_size = [], []
    for path, r in rows.items():
        k = (r['stato'], r['tipo'])
        a = agg[k]
        a[0] += 1
        decl = int(r['size_dichiarata'] or 0)
        a[1] += decl
        if path in real:
            a[2] += real[path]
            if real[path] == 0:
                a[4] += 1
        else:
            a[3] += 1
            missing.append(r)

    extra = [(p, s) for p, s in real.items() if p not in rows]
    extra_old = [(p, s) for p, s in extra if oid_of(p) in cur_oids]
    extra_gone = [(p, s) for p, s in extra if oid_of(p) not in cur_oids]

    os.makedirs(outdir, exist_ok=True)
    out = ['# Blobstorage reale vs Data.fs', '',
           f'- file su disco: {len(real):,} ({human(sum(real.values()))})',
           f'- blob nel Data.fs: {len(rows):,} '
           f'(dichiarati {human(sum(int(r["size_dichiarata"] or 0) for r in rows.values()))})',
           f'- presenti su disco: {sum(1 for p in rows if p in real):,} '
           f'({human(sum(real[p] for p in rows if p in real))})',
           f'- referenziati ma ASSENTI su disco: {len(missing):,}',
           f'- su disco ma non correnti: {len(extra):,} '
           f'({human(sum(s for _, s in extra))}): '
           f'{len(extra_old):,} sono revisioni vecchie di blob ancora vivi '
           f'({human(sum(s for _, s in extra_old))}), '
           f'{len(extra_gone):,} di oggetti non più nel Data.fs '
           f'({human(sum(s for _, s in extra_gone))})', '',
           '| stato | tipo | blob | dichiarata | reale | assenti | zero byte |',
           '|---|---|---|---|---|---|---|']
    for k, a in sorted(agg.items(), key=lambda kv: -kv[1][2]):
        out.append(f'| {k[0]} | {k[1]} | {a[0]:,} | {human(a[1])} | '
                   f'{human(a[2])} | {a[3]:,} | {a[4]:,} |')
    text = '\n'.join(out) + '\n'
    print(text)
    with open(os.path.join(outdir, 'blobstorage.md'), 'w') as f:
        f.write(text)

    with open(os.path.join(outdir, 'missing_blobs.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['oid', 'blob_path', 'stato', 'tipo', 'proprietario',
                    'size_dichiarata'])
        for r in missing:
            w.writerow([r['oid'], r['blob_path'], r['stato'], r['tipo'],
                        r['proprietario'], r['size_dichiarata']])
    with open(os.path.join(outdir, 'extra_blobs.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['blob_path', 'size', 'tipo'])
        for p, s in extra_old:
            w.writerow([p, s, 'revisione-vecchia'])
        for p, s in extra_gone:
            w.writerow([p, s, 'oid-non-nel-datafs'])


if __name__ == '__main__':
    main(*sys.argv[1:4])
