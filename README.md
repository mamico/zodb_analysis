# zodb_analysis

Yet another ZODB analysis tool.

Analyzes a Plone `Data.fs` **without the blobstorage and without importing any Plone/Zope classes**: it reads the raw records, rebuilds the reference graph and decodes pickles with stub classes.

It answers three questions:

1. How many external blob references (`ZODB.blob.Blob`) are there, and how big are they (size declared by the `NamedBlob*` objects)?
2. How many of them are `plone.scale` scales, and how many are "stale" (generated for an earlier modification of the same field)?
3. Which content objects are zombies (outside the containment tree) and what keeps them alive: `portal_historiesstorage`, IntIds, RelationCatalog, other tools.

## Requirements

Python 3.12 and `ZODB` (+ `zodbpickle`).

```bash
uv venv venv && uv pip install -p venv/bin/python ZODB zodbpickle
```

## Usage

```bash
cd tools
python analyze.py <Data.fs> <graph.pickle> <outdir>
```

- Use a **symlink** to `Data.fs` in a writable folder: the FileStorage index is saved next to the link, leaving the original file untouched.
- `graph.pickle` is the graph cache (created on first run, reused afterwards). Delete it if `Data.fs` changes.
- The file is always opened read-only.

For reference: a 6 GB `Data.fs` (8.5 M objects) takes about 10 minutes and a few GB of RAM.

## Output

In `<outdir>`:

| file | content |
|---|---|
| `report.md` | summary tables: classes, reachability, retainers, history, scales, blobs, zombies |
| `blobs.csv` | one blob per row: state, kind, owner, declared size, expected blobstorage path (bushy layout) |
| `histories.csv` | one CMFEditions history per row: alive/deleted, versions, attributed bytes and blobs |
| `zombies.csv` | zombie content objects with category and reference chain |

`blob_path` in `blobs.csv` lets you measure the real files on the server (e.g. `du` per category).

## How it works

- `tools/zodbgraph.py`: graph built from current records (`ZODB.serialize.referencesf`), BTree and OFS container readers.
- `tools/analyze.py`: containment tree from the `Application`, *retainers* (site tools, IntIds, RelationCatalog), attribution of objects and blobs to each retainer, analysis of history, scales, blobs and zombies.

An object is **live** if it is reachable from the root without going through a retainer. All others are attributed to the retainers that reach them.

## Limitations

- Sizes come from the `size` field of `NamedBlob*`, not from disk.
- "Stale" is a heuristic: `modified` older than the latest `modified` of the same field in the same `ScalesDict`.
- Only current revisions are analyzed: it does not measure space reclaimable by a pack.
- Designed for Plone 6 with `plone.namedfile`, `plone.scale`, CMFEditions and `five.intid`.

## Inspiration

[`ZODB/scripts`](https://github.com/zopefoundation/ZODB/tree/master/src/ZODB/scripts) (`analyze`, `referrers`) and [`collective.zodbdebug`](https://github.com/collective/collective.zodbdebug) (reference map and referrer chains).
