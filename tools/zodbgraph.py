"""Shared helpers: build/load the reference graph of a FileStorage and decode
records without importing application (Plone/Zope) classes.

Usage (build cache):
    python zodbgraph.py Data.fs graph.pickle
"""
import pickle
import sys
import time
from io import BytesIO

from ZODB._compat import PersistentUnpickler
from ZODB.FileStorage import FileStorage
from ZODB.serialize import referencesf
from ZODB.utils import get_pickle_metadata, p64, u64


# --------------------------------------------------------------------------
# Graph building
# --------------------------------------------------------------------------

def open_storage(path):
    """Open read-only. If `path` is a symlink placed next to a writable dir,
    the scanned index is saved as <path>.index so later opens are fast."""
    import os
    fs = FileStorage(path, read_only=True)
    if os.path.islink(path) and not os.path.exists(path + '.index'):
        # FileStorage._save_index() is a no-op when read-only: save directly.
        fs._index.save(fs._pos, path + '.index')
    return fs


def build_graph(path, progress=True):
    fs = open_storage(path)
    classes = []
    class_idx = {}
    g = {
        'cls': {},    # oid(int) -> class index
        'size': {},   # oid -> pickle size
        'tid': {},    # oid -> tid (int) of current revision
        'refs': {},   # oid -> tuple(oid, ...)
        'classes': classes,
        'last_tid': u64(fs.lastTransaction()),
        'file_size': fs.getSize(),
    }
    t0 = time.time()
    n = 0
    for oid in fs._index.keys():
        try:
            data, tid = fs.load(oid)
        except Exception as e:  # POSKeyError on undone creations etc.
            print('load error', u64(oid), e, file=sys.stderr)
            continue
        mod, name = get_pickle_metadata(data)
        cname = f'{mod}.{name}'
        ci = class_idx.get(cname)
        if ci is None:
            ci = class_idx[cname] = len(classes)
            classes.append(cname)
        o = u64(oid)
        g['cls'][o] = ci
        g['size'][o] = len(data)
        g['tid'][o] = u64(tid)
        refs = referencesf(data)
        if refs:
            g['refs'][o] = tuple(u64(r) for r in refs)
        n += 1
        if progress and n % 200000 == 0:
            print(f'  {n} records, {time.time() - t0:.0f}s', file=sys.stderr)
    fs.close()
    return g


def load_graph(path):
    # Safe: the cache is produced locally by build_graph() above (plain
    # dicts/tuples/ints/strs). ZODB records themselves are decoded via
    # decode(), whose find_class only returns inert stub classes.
    with open(path, 'rb') as f:
        return pickle.load(f)


# --------------------------------------------------------------------------
# Record decoding with stub classes
# --------------------------------------------------------------------------

class Ref:
    __slots__ = ('oid', 'klass')

    def __init__(self, oid, klass=None):
        self.oid = oid
        self.klass = klass

    def __repr__(self):
        return f'<Ref {self.oid:#x} {self.klass or ""}>'


class Stub:
    """Instance of a class we cannot/do not want to import."""

    def __init__(self, *args, **kw):
        self._args = args

    def __setstate__(self, state):
        self._state = state

    def __repr__(self):
        return f'<{type(self).__module__}.{type(self).__name__}>'


_stub_cache = {}
_SAFE_BUILTINS = {'set', 'frozenset', 'object', 'bytes', 'bytearray', 'list',
                  'dict', 'tuple', 'int', 'float', 'str', 'complex', 'bool'}


def _reconstructor(cls, base, state):
    obj = cls.__new__(cls)
    return obj


def _find_class(module, name):
    if module in ('copy_reg', 'copyreg') and name == '_reconstructor':
        return _reconstructor
    if module in ('__builtin__', 'builtins') and name in _SAFE_BUILTINS:
        import builtins
        return getattr(builtins, name)
    key = (module, name)
    cls = _stub_cache.get(key)
    if cls is None:
        cls = _stub_cache[key] = type(name, (Stub,), {'__module__': module})
    return cls


def _persistent_load(ref):
    if isinstance(ref, tuple):
        oid, klass = ref
        if isinstance(klass, type):
            klass = f'{klass.__module__}.{klass.__name__}'
        elif isinstance(klass, tuple):
            klass = '.'.join(klass)
        return Ref(u64(oid), klass)
    if isinstance(ref, (bytes, str)):
        if isinstance(ref, str):
            ref = ref.encode('latin-1')
        return Ref(u64(ref))
    # list form: ['w', (oid,)] weakref, ['m', ...] multi-db, ['n', ...]
    kind = ref[0]
    args = ref[1]
    if kind in ('w', 'n', 'm'):
        oid = args[0] if kind == 'w' else args[1]
        if isinstance(oid, str):
            oid = oid.encode('latin-1')
        r = Ref(u64(oid), 'weakref' if kind == 'w' else kind)
        return r
    return Ref(-1, repr(ref))


def decode(data):
    """Return (classname, state) of a record pickle."""
    mod, name = get_pickle_metadata(data)
    f = BytesIO(data)
    u = PersistentUnpickler(_find_class, _persistent_load, f)
    u.load()  # class meta
    state = u.load()
    return f'{mod}.{name}', state


class Reader:
    """Load and decode records by integer oid."""

    def __init__(self, path):
        self.fs = open_storage(path)

    def raw(self, oid):
        return self.fs.load(p64(oid))[0]

    def state(self, oid):
        return decode(self.raw(oid))[1]

    # ---- BTrees -------------------------------------------------------
    def btree_items(self, oid_or_state, cname=None):
        """Yield (key, value) from a BTree/Bucket/TreeSet given oid."""
        if isinstance(oid_or_state, Ref):
            oid_or_state = oid_or_state.oid
        if isinstance(oid_or_state, int):
            cname, state = decode(self.raw(oid_or_state))
        else:
            state = oid_or_state
        if state is None:
            return
        is_set = cname.endswith('Set')
        if cname.endswith('Bucket') or cname.endswith('Set') and 'Tree' not in cname:
            yield from self._bucket_chain(state, is_set)
            return
        # Tree
        if len(state) == 1:  # inline single bucket
            yield from self._iter_items(state[0][0][0], is_set)
            return
        _children, firstbucket = state
        b = firstbucket
        while b is not None:
            bstate = self.state(b.oid)
            yield from self._iter_items(bstate[0], is_set)
            b = bstate[1] if len(bstate) > 1 else None

    def _bucket_chain(self, state, is_set):
        while True:
            yield from self._iter_items(state[0], is_set)
            if len(state) > 1 and state[1] is not None:
                state = self.state(state[1].oid)
            else:
                break

    @staticmethod
    def _iter_items(items, is_set):
        if is_set:
            for k in items:
                yield k, None
        else:
            for i in range(0, len(items), 2):
                yield items[i], items[i + 1]

    # ---- OFS / CMF containers ----------------------------------------
    def children(self, oid):
        """Return {id: Ref} for an OFS ObjectManager / BTreeFolder."""
        st = self.state(oid)
        out = {}
        if not isinstance(st, dict):
            return out
        tree = st.get('_tree')
        if isinstance(tree, Ref):
            for k, v in self.btree_items(tree.oid):
                if isinstance(v, Ref):
                    out[k] = v
        for entry in st.get('_objects', ()) or ():
            if isinstance(entry, dict):
                i = entry.get('id')
                v = st.get(i)
                if isinstance(v, Ref):
                    out[i] = v
        return out


if __name__ == '__main__':
    src, dst = sys.argv[1], sys.argv[2]
    t0 = time.time()
    g = build_graph(src)
    with open(dst, 'wb') as f:
        pickle.dump(g, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f'{len(g["cls"])} objects, {len(g["classes"])} classes, '
          f'{time.time() - t0:.0f}s -> {dst}')
