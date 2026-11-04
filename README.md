# zodb_analysis

Yet another ZODB analysis tool.

Analizza un `Data.fs` di Plone **senza blobstorage e senza importare classi Plone/Zope**: legge i record grezzi, ricostruisce il grafo dei riferimenti e decodifica i pickle con classi stub.

Risponde a tre domande:

1. Quanti riferimenti a blob esterni (`ZODB.blob.Blob`) ci sono, e quanto pesano (dimensione dichiarata dai `NamedBlob*`)?
2. Quanti sono scale di `plone.scale`, e quante sono "stale" (generate per una modifica precedente dello stesso campo)?
3. Quali contenuti sono zombie (fuori dall'albero di contenimento) e cosa li tiene in vita: `portal_historiesstorage`, IntIds, RelationCatalog, altri tool.

## Requisiti

Python 3.12 e `ZODB` (+ `zodbpickle`).

```bash
uv venv venv && uv pip install -p venv/bin/python ZODB zodbpickle
```

## Uso

```bash
cd tools
python analyze.py <Data.fs> <graph.pickle> <outdir>
```

- Usa un **symlink** a `Data.fs` in una cartella scrivibile: l'indice FileStorage viene salvato accanto al link, senza toccare il file originale.
- `graph.pickle` è la cache del grafo (creata al primo run, riusata dopo). Va rigenerata se cambia il `Data.fs`.
- Il file viene aperto in sola lettura.

Riferimento: un `Data.fs` da 6 GB (8,5 M oggetti) richiede circa 10 minuti e qualche GB di RAM.

## Output

In `<outdir>`:

| file | contenuto |
|---|---|
| `report.md` | tabelle riassuntive: classi, raggiungibilità, retainer, history, scale, blob, zombie |
| `blobs.csv` | un blob per riga: stato, tipo, proprietario, size dichiarata, path atteso in blobstorage (layout bushy) |
| `histories.csv` | una history CMFEditions per riga: vivo/cancellato, versioni, byte e blob attribuiti |
| `zombies.csv` | contenuti zombie con categoria e catena di riferimenti |

`blob_path` in `blobs.csv` permette di misurare i file reali sul server (es. `du` per categoria).

## Come funziona

- `tools/zodbgraph.py`: grafo dai record correnti (`ZODB.serialize.referencesf`), lettura BTree e contenitori OFS.
- `tools/analyze.py`: albero di contenimento dalla `Application`, *retainer* (tool del sito, IntIds, RelationCatalog), attribuzione di oggetti e blob a ciascun retainer, analisi di history, scale, blob e zombie.

Un oggetto è **live** se raggiungibile dalla root senza passare dai retainer. Gli altri sono attribuiti ai retainer che li raggiungono.

## Limiti

- Dimensioni da campo `size` dei `NamedBlob*`, non da disco.
- "Stale" è un'euristica: `modified` più vecchio dell'ultimo `modified` dello stesso campo nello stesso `ScalesDict`.
- Analizza solo le revisioni correnti: non misura lo spazio recuperabile con una pack.
- Pensato per Plone 6 con `plone.namedfile`, `plone.scale`, CMFEditions e `five.intid`.

## Ispirazione

[`ZODB/scripts`](https://github.com/zopefoundation/ZODB/tree/master/src/ZODB/scripts) (`analyze`, `referrers`) e [`collective.zodbdebug`](https://github.com/collective/collective.zodbdebug) (mappa dei riferimenti e catena dei referrer).
