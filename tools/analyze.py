"""Analisi di un Data.fs Plone senza blobstorage e senza classi Plone.

Domande:
  * quanti blob sono referenziati (e quali file servirebbero in blobstorage)
  * quanti sono scale (plone.scale ScalesDict), quante scale sono "stale"
  * quali oggetti sono zombie (contenuti non raggiungibili via contenimento)
    e cosa li tiene in vita: portal_historiesstorage, intids, relation
    catalog, altri tool, riferimenti da oggetti vivi, o nessuno (garbage)

Usage:
  python analyze.py <Data.fs (meglio un symlink)> <graph.pickle> <outdir>
Il grafo viene costruito e salvato in graph.pickle se non esiste.
"""
import csv
import os
import pickle
import sys
import time
from collections import Counter, defaultdict, deque

from ZODB.blob import BushyLayout
from ZODB.TimeStamp import TimeStamp
from ZODB.utils import p64

from zodbgraph import Reader, Ref, Stub, build_graph, load_graph

LAYOUT = BushyLayout()

BLOB = 'ZODB.blob.Blob'
NB_CLASSES = {'plone.namedfile.file.NamedBlobImage',
              'plone.namedfile.file.NamedBlobFile'}
SCALES = 'plone.scale.storage.ScalesDict'
UID_ANN = 'Products.CMFUid.UniqueIdAnnotationTool.UniqueIdAnnotation'
SIDE_RETAINER_CLASSES = {
    'five.intid.intid.IntIds', 'zope.intid.IntIds',
    'z3c.relationfield.index.RelationCatalog', 'zc.relation.catalog.Catalog',
}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)


def tid2str(tid):
    return str(TimeStamp(p64(tid)))[:19]


def human(n):
    n = float(n)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024:
            return f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.1f} PB'


class Union:
    def __init__(self, *sets):
        self.sets = sets

    def __contains__(self, x):
        return any(x in s for s in self.sets)


def bfs(starts, refs, exists, stop=(), parent=None):
    seen = set()
    dq = deque()
    for s in starts:
        if s in exists and s not in seen:
            seen.add(s)
            dq.append(s)
    while dq:
        o = dq.popleft()
        for r in refs.get(o, ()):
            if r in seen or r not in exists:
                continue
            if parent is not None and r not in parent:
                parent[r] = o
            if r in stop:
                continue
            seen.add(r)
            dq.append(r)
    return seen


def short_cls(c):
    return c.rsplit('.', 1)[-1]


class Analysis:

    def __init__(self, fs_path, graph_path, outdir):
        self.outdir = outdir
        os.makedirs(outdir, exist_ok=True)
        if os.path.exists(graph_path):
            log('loading graph', graph_path)
            self.g = load_graph(graph_path)
        else:
            log('building graph (full pass over current records)...')
            self.g = build_graph(fs_path)
            with open(graph_path, 'wb') as f:
                pickle.dump(self.g, f, protocol=pickle.HIGHEST_PROTOCOL)
        self.R = Reader(fs_path)
        self.classes = self.g['classes']
        self.cls = self.g['cls']
        self.refs = self.g['refs']
        self.size = self.g['size']
        self.tid = self.g['tid']
        self.out = []  # report lines (markdown)

    # ------------------------------------------------------------------
    def cname(self, o):
        return self.classes[self.cls[o]]

    def p(self, *lines):
        for line in lines:
            self.out.append(line)
            print(line, flush=True)

    def table(self, header, rows):
        self.p('| ' + ' | '.join(header) + ' |',
               '|' + '|'.join('---' for _ in header) + '|')
        for r in rows:
            self.p('| ' + ' | '.join(str(x) for x in r) + ' |')
        self.p('')

    def state(self, o):
        try:
            return self.R.state(o)
        except Exception as e:  # noqa
            return None

    # ------------------------------------------------------------------
    def run(self):
        g = self.g
        cls, refs = self.cls, self.refs
        self.p('# Analisi Data.fs', '')
        self.p(f'- dimensione file: {human(g["file_size"])}',
               f'- ultima transazione: {tid2str(g["last_tid"])}',
               f'- oggetti correnti: {len(cls):,}',
               f'- classi distinte: {len(self.classes):,}', '')

        # -- class stats --------------------------------------------------
        self.by_class = defaultdict(list)
        for o, c in cls.items():
            self.by_class[c].append(o)
        cbytes = Counter()
        for o, c in cls.items():
            cbytes[c] += self.size[o]
        self.p('## Classi principali (oggetti correnti)', '')
        top = sorted(self.by_class, key=lambda c: -cbytes[c])[:30]
        self.table(['classe', 'oggetti', 'byte pickle'],
                   [(self.classes[c], f'{len(self.by_class[c]):,}',
                     human(cbytes[c])) for c in top])

        # -- reachability --------------------------------------------------
        log('reachability from root...')
        self.reach_all = bfs([0], refs, cls)
        garbage = [o for o in cls if o not in self.reach_all]
        dangling = Counter()
        dangling_targets = set()
        for src, rs in refs.items():
            for r in rs:
                if r not in cls:
                    dangling[self.cname(src)] += 1
                    dangling_targets.add(r)
        self.p('## Raggiungibilità dalla root', '')
        gb = sum(self.size[o] for o in garbage)
        self.p(f'- raggiungibili: {len(self.reach_all):,}',
               f'- NON raggiungibili (garbage, li toglie una pack con GC): '
               f'{len(garbage):,} oggetti, {human(gb)} di pickle',
               f'- riferimenti pendenti (oid referenziati ma assenti, '
               f'POSKeyError): {sum(dangling.values()):,} riferimenti verso '
               f'{len(dangling_targets):,} oid', '')
        if garbage:
            gc = Counter(self.cname(o) for o in garbage)
            self.table(['classe garbage', 'oggetti'], gc.most_common(20))
        if dangling:
            self.table(['classe che referenzia oid mancanti', 'riferimenti'],
                       dangling.most_common(15))

        # -- content classes ------------------------------------------------
        log('detecting content classes...')
        self.is_content = {}
        for c, oids in self.by_class.items():
            name = self.classes[c]
            if name.startswith(('BTrees.', 'persistent.', 'ZODB.',
                                'Persistence.', 'Products.ZopeVersionControl',
                                'plone.namedfile', 'plone.scale')):
                self.is_content[c] = False
                continue
            hit = False
            for o in oids[:3]:
                st = self.state(o)
                if isinstance(st, dict) and 'portal_type' in st:
                    hit = True
                    break
            self.is_content[c] = hit
        self.content_classes = {self.classes[c] for c, v in
                                self.is_content.items() if v}

        # -- containment walk -----------------------------------------------
        log('walking containment tree...')
        root_state = self.state(0)
        app = root_state['data']['Application'].oid
        self.app = app
        paths = {app: ''}
        stack = [app]
        while stack:
            o = stack.pop()
            try:
                ch = self.R.children(o)
            except Exception:  # noqa
                continue
            for k, v in ch.items():
                if v.oid in paths or v.oid not in cls:
                    continue
                paths[v.oid] = f'{paths[o]}/{k}'
                stack.append(v.oid)
        self.paths = paths
        app_children = self.R.children(app)
        sites = {v.oid: k for k, v in app_children.items()
                 if 'PloneSite' in self.cname(v.oid)
                 or 'Portal' in self.cname(v.oid)}
        self.sites = sites

        # -- side retainers ---------------------------------------------------
        S = {}
        for k, v in app_children.items():
            if v.oid not in sites and v.oid in cls:
                S[v.oid] = f'/{k}'
        for site in sites:
            for k, v in self.R.children(site).items():
                if v.oid in cls and not self.is_content[cls[v.oid]]:
                    S[v.oid] = paths.get(v.oid, k)
        for c, oids in self.by_class.items():
            if self.classes[c] in SIDE_RETAINER_CLASSES:
                for o in oids:
                    if o in self.reach_all:
                        S[o] = (f'{short_cls(self.classes[c])}'
                                f'({paths.get(o, hex(o))})')
        self.S = S

        log(f'live set (root, not entering {len(S)} tools/retainers)...')
        par = {}
        L = bfs([0], refs, cls, stop=S, parent=par)
        self.L = L
        log('per-retainer exclusive sets...')
        member = defaultdict(list)
        stopLS = Union(L, S)
        for s in sorted(S, key=lambda x: S[x]):
            E = bfs([s], refs, cls, stop=stopLS, parent=par)
            for n in E:
                member[n].append(s)
        self.member = member
        self.par = par

        def status(o):
            if o not in self.reach_all:
                return 'garbage'
            if o in L:
                return 'live'
            m = member.get(o)
            if not m:
                return '?'
            return '+'.join(S[x] for x in m)
        self.status = status

        # retainer table
        ret_cnt = Counter()
        ret_bytes = Counter()
        ret_blobs = Counter()
        ret_content = Counter()
        for n, m in member.items():
            key = '+'.join(S[x] for x in m) if len(m) > 1 else S[m[0]]
            ret_cnt[key] += 1
            ret_bytes[key] += self.size[n]
            c = self.cname(n)
            if c == BLOB:
                ret_blobs[key] += 1
            if self.is_content[cls[n]]:
                ret_content[key] += 1
        self.p('## Cosa tiene in vita gli oggetti', '',
               'Insieme *live* = raggiungibile dalla root senza passare dai '
               f'tool/retainer ({len(S)}: tool del sito, figli della app, '
               'IntIds, RelationCatalog). Per ogni retainer: oggetti '
               'raggiungibili SOLO attraverso di esso.', '',
               f'- live: {len(L):,} oggetti, '
               f'{human(sum(self.size[o] for o in L))}', '')
        rows = []
        for key in sorted(ret_cnt, key=lambda k: -ret_bytes[k])[:40]:
            rows.append((key, f'{ret_cnt[key]:,}', human(ret_bytes[key]),
                         f'{ret_content[key]:,}', f'{ret_blobs[key]:,}'))
        self.table(['retainer', 'oggetti esclusivi', 'byte pickle',
                    'di cui contenuti', 'di cui Blob'], rows)

        self.content_owner_labels()
        self.history()
        self.scales()
        self.blobs()
        self.zombies()

        with open(os.path.join(self.outdir, 'report.md'), 'w') as f:
            f.write('\n'.join(self.out) + '\n')
        log('done ->', self.outdir)

    # ------------------------------------------------------------------
    def content_owner_labels(self):
        """label[node] = nearest content object owning it (top-down)."""
        log('labelling owners...')
        cls, refs, S = self.cls, self.refs, self.S
        isc = self.is_content
        label = {}
        content = [o for o, c in cls.items() if isc[c]]
        live = [o for o in content if o in self.paths]
        other = [o for o in content if o not in self.paths]
        for group in (live, other):
            dq = deque()
            for c in group:
                if c not in label:
                    label[c] = c
                    dq.append(c)
            while dq:
                o = dq.popleft()
                lab = label[o]
                for r in refs.get(o, ()):
                    if (r in label or r not in cls or r in S
                            or isc[cls[r]]):
                        continue
                    label[r] = lab
                    dq.append(r)
        self.label = label
        self.content = content

    # ------------------------------------------------------------------
    def history(self):
        log('analysing portal_historiesstorage...')
        self.copy_hid = {}
        self.hist_label = {}
        self.hist_info = {}
        self.hist_bytes = Counter()
        self.hist_objs = Counter()
        self.hist_blob = Counter()
        self.hist_blob_bytes = Counter()
        self.orphan_hids = set()
        self.hist_path = None
        self.live_uid_path = {}
        cls, refs = self.cls, self.refs
        # live cmf_uid -> path
        for o in self.by_class.get(self.classes.index(UID_ANN)
                                   if UID_ANN in self.classes else -1, []):
            owner = self.label.get(o)
            if owner in self.paths:
                st = self.state(o)
                if isinstance(st, dict) and '_uid' in st:
                    self.live_uid_path[st['_uid']] = self.paths[owner]

        hs = [o for o, pth in self.paths.items()
              if pth.endswith('/portal_historiesstorage')]
        if not hs:
            self.p('## portal_historiesstorage', '', 'non trovato', '')
            return
        hs = hs[0]
        self.hist_path = self.S.get(hs)
        st = self.state(hs)
        shadow = self.state(st['_shadowStorage'].oid)['_storage']
        repo = self.state(st['zvc_repo'].oid)
        zvc = dict(self.R.btree_items(repo['_histories'].oid))
        n_versions = 0
        n_removed = 0
        for hid, sh in self.R.btree_items(shadow.oid):
            shs = self.state(sh.oid) or {}
            full = shs.get('_full')
            versions = list(self.R.btree_items(full.oid)) if isinstance(
                full, Ref) else []
            zvc_id = None
            last_meta = {}
            removed = 0
            for vid, v in versions:
                if isinstance(v, Ref) or not isinstance(v, dict):
                    removed += 1
                    continue
                vi = v.get('vc_info')
                if zvc_id is None and isinstance(vi, Ref):
                    vis = self.state(vi.oid)
                    if isinstance(vis, dict):
                        zvc_id = vis.get('history_id')
                elif zvc_id is None and isinstance(vi, Stub):
                    zvc_id = (getattr(vi, '_state', None) or {}).get(
                        'history_id')
                md = v.get('metadata')
                if isinstance(md, dict):
                    last_meta = md.get('sys_metadata', md)
            n_versions += len(versions)
            n_removed += removed
            starts = [sh.oid]
            zh = zvc.get(zvc_id)
            if isinstance(zh, Ref):
                starts.append(zh.oid)
            self.hist_info[hid] = {
                'versions': len(versions), 'removed': removed,
                'zvc_id': zvc_id, 'starts': starts,
                'live_path': self.live_uid_path.get(hid),
                'last_path': last_meta.get('path') if isinstance(
                    last_meta, dict) else None,
                'last_ts': last_meta.get('timestamp') if isinstance(
                    last_meta, dict) else None,
                'principal': last_meta.get('principal') if isinstance(
                    last_meta, dict) else None,
            }
        # attribute history nodes (and version copies) to history ids
        isc = self.is_content
        for hid, info in self.hist_info.items():
            dq = deque()
            for s in info['starts']:
                if s in cls and s not in self.hist_label:
                    self.hist_label[s] = hid
                    dq.append(s)
            while dq:
                o = dq.popleft()
                for r in refs.get(o, ()):
                    if r not in cls or r in self.hist_label or r in self.S:
                        continue
                    if isc[cls[r]]:
                        self.copy_hid.setdefault(r, hid)
                        continue
                    self.hist_label[r] = hid
                    dq.append(r)
        # byte/blob attribution per history
        hist_bytes = Counter()
        hist_objs = Counter()
        for o, hid in self.hist_label.items():
            hist_bytes[hid] += self.size[o]
            hist_objs[hid] += 1
        for o, hid in self.copy_hid.items():
            for n in [o] + [x for x in self._owned(o)]:
                hist_bytes[hid] += self.size[n]
                hist_objs[hid] += 1
        self.hist_bytes = hist_bytes
        orphan = [h for h, i in self.hist_info.items() if not i['live_path']]
        self.p('## portal_historiesstorage (CMFEditions)', '',
               f'- history: {len(self.hist_info):,} '
               f'(versioni totali {n_versions:,}, di cui purged/Removed '
               f'{n_removed:,})',
               f'- history di contenuti NON più presenti nel sito '
               f'(cmf_uid senza contenuto vivo): {len(orphan):,}',
               f'- copie di contenuti salvate nelle versioni: '
               f'{len(self.copy_hid):,}',
               f'- byte pickle delle history orfane: '
               f'{human(sum(hist_bytes[h] for h in orphan))} su '
               f'{human(sum(hist_bytes.values()))}', '')
        self.hist_objs = hist_objs
        self.orphan_hids = set(orphan)

    def _owned(self, content_oid):
        """Non-content nodes labelled with this content owner (cached)."""
        if not hasattr(self, '_owned_idx'):
            idx = defaultdict(list)
            for n, lab in self.label.items():
                if n != lab:
                    idx[lab].append(n)
            self._owned_idx = idx
        return self._owned_idx.get(content_oid, ())

    # ------------------------------------------------------------------
    def scales(self):
        log('analysing scales...')
        self.scale_nb = {}   # NamedBlobImage oid -> info dict
        sc = self.classes.index(SCALES) if SCALES in self.classes else None
        n_dicts = 0
        n_entries = 0
        n_stale = 0
        per_dict = Counter()
        for o in self.by_class.get(sc, []) if sc is not None else []:
            st = self.state(o)
            data = st.get('data') if isinstance(st, dict) else None
            if not isinstance(data, dict):
                continue
            n_dicts += 1
            maxmod = defaultdict(int)
            for e in data.values():
                if isinstance(e, dict):
                    m = e.get('modified') or 0
                    fn = e.get('fieldname')
                    maxmod[fn] = max(maxmod[fn], m)
            cnt = 0
            for e in data.values():
                if not isinstance(e, dict):
                    continue
                nb = e.get('data')
                n_entries += 1
                cnt += 1
                stale = (e.get('modified') or 0) < maxmod[e.get('fieldname')]
                n_stale += stale
                if isinstance(nb, Ref):
                    self.scale_nb[nb.oid] = {
                        'stale': stale, 'w': e.get('width'),
                        'h': e.get('height'), 'field': e.get('fieldname'),
                        'scalesdict': o}
            per_dict[min(cnt, 50)] += 1
        self.p('## Scale (plone.scale ScalesDict)', '',
               f'- ScalesDict: {n_dicts:,}',
               f'- scale registrate: {n_entries:,} '
               f'(media {n_entries / max(n_dicts, 1):.1f} per ScalesDict)',
               f'- scale "stale" (generazione vecchia: modified < ultimo '
               f'modified dello stesso campo): {n_stale:,}', '')
        self.table(['scale per ScalesDict', 'ScalesDict'],
                   sorted(per_dict.items()))

    # ------------------------------------------------------------------
    def blobs(self):
        log('analysing blobs...')
        cls, refs = self.cls, self.refs
        bc = self.classes.index(BLOB) if BLOB in self.classes else None
        blobs = set(self.by_class.get(bc, [])) if bc is not None else set()
        nbc = {self.classes.index(c) for c in NB_CLASSES
               if c in self.classes}
        nbs = set()
        for c in nbc:
            nbs.update(self.by_class[c])
        rev = defaultdict(list)
        for src, rs in refs.items():
            for r in rs:
                if r in blobs:
                    rev[r].append(src)
        nbinfo = {}
        for nb in nbs:
            st = self.state(nb)
            if isinstance(st, dict):
                nbinfo[nb] = st
        rows = []
        summary = Counter()
        summary_bytes = Counter()
        for b in sorted(blobs):
            referrers = rev.get(b, [])
            nb_refs = [r for r in referrers if r in nbs]
            other = [self.cname(r) for r in referrers if r not in nbs]
            st = self.status(b)
            if self.hist_path and st == self.hist_path:
                st_kind = 'solo-history'
            elif self.hist_path and self.hist_path in st:
                st_kind = 'history+altri-retainer'
            elif st == 'live':
                st_kind = 'live'
            elif st == 'garbage':
                st_kind = 'garbage'
            else:
                st_kind = 'altro-retainer'
            sc = [self.scale_nb[n] for n in nb_refs if n in self.scale_nb]
            if sc:
                kind = 'scale-stale' if all(s['stale'] for s in sc) else 'scale'
            elif nb_refs:
                kind = 'file/immagine'
            elif other:
                kind = 'altro:' + short_cls(other[0])
            else:
                kind = 'nessun-referrer'
            info = nbinfo.get(nb_refs[0], {}) if nb_refs else {}
            declared = info.get('size') or 0
            # owner
            lab = self.label.get(b)
            hid = self.hist_label.get(b)
            owner = ''
            if lab is not None and lab in self.paths:
                owner = self.paths[lab]
            elif lab is not None and lab in self.copy_hid:
                hid = self.copy_hid[lab]
            if hid is not None and not owner:
                hi = self.hist_info.get(hid, {})
                owner = (f'history:{hid} '
                         f'{"vivo " + hi["live_path"] if hi.get("live_path") else "CANCELLATO " + str(hi.get("last_path") or "")}')
                self.hist_blob[hid] += 1
                self.hist_blob_bytes[hid] += declared
                if st_kind == 'solo-history' and hid in self.orphan_hids:
                    st_kind = 'solo-history-orfana'
            elif lab is not None and not owner:
                owner = f'contenuto-non-contenuto oid={lab:#x}'
            also_hist = any(r not in self.L for r in nb_refs) and st == 'live'
            summary[(st_kind, kind)] += 1
            summary_bytes[(st_kind, kind)] += declared
            sinfo = sc[0] if sc else {}
            rows.append([
                f'{b:#x}', f'{self.tid[b]:#018x}',
                LAYOUT.getBlobFilePath(p64(b), p64(self.tid[b])),
                st_kind, kind, st if st_kind == 'altro-retainer' else '',
                owner, info.get('filename', ''),
                info.get('contentType', ''), declared,
                sinfo.get('field', ''),
                f'{sinfo["w"]}x{sinfo["h"]}' if sinfo else '',
                'si' if also_hist else '', tid2str(self.tid[b])])
        with open(os.path.join(self.outdir, 'blobs.csv'), 'w',
                  newline='') as f:
            w = csv.writer(f)
            w.writerow(['oid', 'tid', 'blob_path', 'stato', 'tipo',
                        'retainer', 'proprietario', 'filename',
                        'content_type', 'size_dichiarata', 'campo_scala',
                        'dimensioni_scala', 'condiviso_con_history',
                        'data_tid'])
            w.writerows(rows)
        tot_decl = sum(summary_bytes.values())
        self.p('## Blob', '',
               f'- record ZODB.blob.Blob correnti: {len(blobs):,} '
               f'(= file in blobstorage necessari, uno per blob alla '
               f'revisione corrente)',
               f'- dimensione totale dichiarata (campo size dei '
               f'NamedBlob*): {human(tot_decl)}',
               '- stati: live = raggiungibile senza tool; solo-history = '
               'tenuto in vita solo da portal_historiesstorage '
               '(solo-history-orfana = di un contenuto cancellato); '
               'garbage = irraggiungibile', '')
        self.table(['stato', 'tipo', 'blob', 'size dichiarata'],
                   [(k[0], k[1], f'{v:,}', human(summary_bytes[k]))
                    for k, v in sorted(summary.items(),
                                       key=lambda kv: -summary_bytes[kv[0]])])
        shared = sum(1 for r in rows if r[12] == 'si')
        self.p(f'- blob live condivisi anche con le versioni '
               f'(stesso Blob referenziato da una copia in history): '
               f'{shared:,}', '')
        # write histories.csv now that blob counts are known
        with open(os.path.join(self.outdir, 'histories.csv'), 'w',
                  newline='') as f:
            w = csv.writer(f)
            w.writerow(['history_id', 'stato', 'path_attuale',
                        'path_ultima_versione', 'versioni', 'purged',
                        'ultima_versione', 'utente', 'oggetti',
                        'byte_pickle', 'blob', 'byte_blob_dichiarati'])
            for hid, i in sorted(self.hist_info.items(),
                                 key=lambda kv: -self.hist_blob_bytes[kv[0]]):
                ts = i['last_ts']
                ts = time.strftime('%Y-%m-%d', time.localtime(ts)) if \
                    isinstance(ts, (int, float)) else ''
                w.writerow([hid, 'vivo' if i['live_path'] else 'CANCELLATO',
                            i['live_path'] or '', i['last_path'] or '',
                            i['versions'], i['removed'], ts,
                            i['principal'] or '', self.hist_objs[hid],
                            self.hist_bytes[hid],
                            self.hist_blob[hid], self.hist_blob_bytes[hid]])
        orph = self.orphan_hids
        self.p(f'- blob in history orfane: '
               f'{sum(self.hist_blob[h] for h in orph):,} '
               f'({human(sum(self.hist_blob_bytes[h] for h in orph))} '
               f'dichiarati)', '')

    # ------------------------------------------------------------------
    def chain(self, o, maxlen=40):
        out = []
        seen = set()
        while o is not None and o not in seen and len(out) < maxlen:
            seen.add(o)
            lbl = short_cls(self.cname(o)) if o in self.cls else '?'
            if o in self.paths:
                lbl += f'[{self.paths[o] or "/"}]'
                out.append(lbl)
                break
            out.append(lbl)
            o = self.par.get(o)
        return ' <- '.join(out)

    def zombies(self):
        log('analysing zombie content...')
        rows = []
        cat = Counter()
        cat_types = defaultdict(Counter)
        for o in self.content:
            if o in self.paths or o in self.copy_hid:
                continue
            st = self.status(o)
            if st == 'live':
                kind = 'live-ma-fuori-albero (referenziato da oggetto vivo)'
            elif st == 'garbage':
                kind = 'garbage (irraggiungibile)'
            else:
                kind = 'tenuto da ' + st
            s = self.state(o) or {}
            pt = s.get('portal_type', '') if isinstance(s, dict) else ''
            cat[kind] += 1
            cat_types[kind][pt] += 1
            owned = self._owned(o)
            nblob = sum(1 for n in owned if self.cname(n) == BLOB)
            rows.append([f'{o:#x}', kind, pt, s.get('id', ''),
                         str(s.get('title', ''))[:120],
                         s.get('_plone.uuid', ''), nblob,
                         tid2str(self.tid[o]),
                         self.chain(o) if st != 'garbage' else ''])
        with open(os.path.join(self.outdir, 'zombies.csv'), 'w',
                  newline='') as f:
            w = csv.writer(f)
            w.writerow(['oid', 'categoria', 'portal_type', 'id', 'title',
                        'uuid', 'blob_posseduti', 'ultima_modifica',
                        'catena_di_riferimenti'])
            w.writerows(rows)
        self.p('## Contenuti zombie', '',
               'Oggetti contenuto (con portal_type) non presenti '
               "nell'albero di contenimento e che non sono copie di "
               'versione in portal_historiesstorage.', '')
        self.table(['categoria', 'oggetti', 'portal_type principali'],
                   [(k, f'{v:,}', ', '.join(f'{t}:{n}' for t, n in
                                           cat_types[k].most_common(5)))
                    for k, v in cat.most_common()])
        # example chains
        ex = defaultdict(list)
        for r in rows:
            if r[8] and len(ex[r[1]]) < 3:
                ex[r[1]].append(r[8])
        if ex:
            self.p('Esempi di catene di riferimento (oggetto <- ... <- '
                   'radice/tool):', '')
            for k, v in ex.items():
                self.p(f'- **{k}**')
                for c in v:
                    self.p(f'  - `{c}`')
            self.p('')


if __name__ == '__main__':
    Analysis(sys.argv[1], sys.argv[2], sys.argv[3]).run()
