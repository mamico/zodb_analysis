"""Remove obsolete plone.scale scales from live content and CMFEditions history.

Run with a full Plone instance (all products installed), DRY-RUN by default:

    bin/instance -OPlone run scripts/cleanup_scales.py
    bin/instance -OPlone run scripts/cleanup_scales.py --commit

Options: see `--help`. Stop other writers (or accept some ConflictError
retries) while it runs. Disk space is only released by a pack (with blob GC).

What it does
------------
Live content (found through the catalog):
    Per ScalesDict ('plone.scale' annotation) and per field, always KEEP the
    latest generation (the scales with the highest `modified`) and delete the
    ones older than `latest - keep_days`.  This is a subset of what
    plone.scale's own `AnnotationStorage._cleanup` would delete, but it never
    removes the scales the catalog metadata (Volto `image_scales`) most
    likely points to.  Legacy tuple keys are removed too.

History (portal_historiesstorage, every version of every history):
    --history drop   remove the whole 'plone.scale' annotation (default;
                     scales are a cache, regenerated on demand)
    --history stale  same rule as live content
    --history skip   leave history alone

Safety
------
* Never writes to the content objects themselves (that would change
  `_p_mtime`, which plone.namedfile uses as "modified" and would invalidate
  all scales): only ScalesDict / __annotations__ are touched.
* No events, no reindexing, no modification date changes.
* Work is split in batches; each batch is committed (or aborted in dry-run)
  and retried on ConflictError.
"""
import argparse
import logging
import sys
import time
from collections import Counter

KEY = "plone.scale"
DAY_MS = 24 * 60 * 60 * 1000
NUMBER = (int, float)

log = logging.getLogger("cleanup_scales")


# --------------------------------------------------------------------------
# Pure logic (no Zope imports, unit-testable)
# --------------------------------------------------------------------------

def obsolete_keys(scales, keep_ms=DAY_MS):
    """Return the keys of `scales` (a ScalesDict-like mapping) to delete.

    Per fieldname the newest generation is kept; entries older than
    `newest - keep_ms` are obsolete.  Entries with an unknown `modified`
    are kept; legacy tuple keys are obsolete.
    """
    latest = {}
    for key, info in scales.items():
        if isinstance(info, dict) and isinstance(info.get("modified"), NUMBER):
            field = info.get("fieldname")
            latest[field] = max(latest.get(field, 0), info["modified"])
    out = []
    for key, info in scales.items():
        if isinstance(key, tuple):
            out.append(key)
            continue
        if not isinstance(info, dict):
            continue
        modified = info.get("modified")
        if not isinstance(modified, NUMBER):
            continue
        if modified < latest[info.get("fieldname")] - keep_ms:
            out.append(key)
    return out


def scale_bytes(info):
    """Declared size of the NamedBlobImage held by a scale entry."""
    data = info.get("data") if isinstance(info, dict) else None
    return getattr(data, "size", 0) or 0


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------
# Work on one object
# --------------------------------------------------------------------------

def clean_annotations(obj, mode, keep_ms, dry, sizes, stats):
    """`mode` is 'stale' or 'drop'. `obj` is a content object or a version copy."""
    from Acquisition import aq_base

    annotations = getattr(aq_base(obj), "__annotations__", None)
    if annotations is None:
        return
    scales = annotations.get(KEY)
    if scales is None:
        return
    try:
        items = list(scales.items())
    except Exception:  # not a mapping: leave it alone
        stats["unreadable"] += 1
        return
    stats["objects_with_scales"] += 1
    stats["scales"] += len(items)
    if mode == "drop":
        keys = [k for k, _ in items]
    else:
        keys = obsolete_keys(scales, keep_ms)
    if not keys:
        return
    stats["objects_touched"] += 1
    stats["deleted"] += len(keys)
    if sizes:
        by_key = dict(items)
        stats["deleted_bytes"] += sum(scale_bytes(by_key[k]) for k in keys)
    if dry:
        return
    if mode == "drop":
        del annotations[KEY]
    else:
        for key in keys:
            del scales[key]


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------

def run_batches(app, items, work, label, args, stats):
    import transaction
    from ZODB.POSException import ConflictError

    total = len(items)
    done = 0
    started = time.time()
    for chunk in chunks(items, args.batch):
        for attempt in range(1, args.retries + 1):
            local = Counter()
            try:
                for item in chunk:
                    work(item, local)
                if args.commit:
                    transaction.get().note(f"cleanup_scales: {label}")
                    transaction.commit()
                else:
                    transaction.abort()
                stats.update(local)
                break
            except ConflictError:
                transaction.abort()
                log.warning("%s: ConflictError, retry %d/%d", label, attempt,
                            args.retries)
        else:
            stats["failed_batches"] += 1
            stats["failed_items"] += len(chunk)
            log.error("%s: batch failed after %d retries", label, args.retries)
        app._p_jar.cacheMinimize()
        done += len(chunk)
        log.info("%s: %d/%d (%.0f%%) %.0fs scales=%d deleted=%d",
                 label, done, total, 100.0 * done / max(total, 1),
                 time.time() - started, stats["scales"], stats["deleted"])


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--commit", action="store_true",
                   help="really write (default: dry-run, nothing is changed)")
    p.add_argument("--site", default="Plone", help="Plone site id")
    p.add_argument("--live", choices=("stale", "skip"), default="stale",
                   help="live content: delete obsolete scales (default) or skip")
    p.add_argument("--history", choices=("drop", "stale", "skip"),
                   default="drop",
                   help="history: drop all scales (default), obsolete only, or skip")
    p.add_argument("--keep-days", type=float, default=1.0,
                   help="grace period before the newest generation (default 1, "
                        "like plone.scale)")
    p.add_argument("--batch", type=int, default=500,
                   help="objects (live) or histories (history) per transaction")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--sizes", action="store_true",
                   help="also sum the declared size of deleted scales "
                        "(loads every scale object: slower)")
    p.add_argument("--limit", type=int, default=0,
                   help="process only the first N items per phase (testing)")
    return p.parse_args(argv)


def get_portal(app, name):
    if hasattr(app, "portal_catalog"):  # -OPlone: app is already the site
        return app
    return app.unrestrictedTraverse(name)


def main(app, argv):
    from Products.CMFCore.utils import getToolByName
    from zope.component.hooks import setSite

    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    keep_ms = int(args.keep_days * DAY_MS)
    portal = get_portal(app, args.site)
    setSite(portal)
    mode = "COMMIT" if args.commit else "DRY-RUN"
    log.info("%s site=%s live=%s history=%s keep_days=%s", mode,
             "/".join(portal.getPhysicalPath()), args.live, args.history,
             args.keep_days)

    live_stats = Counter()
    hist_stats = Counter()

    if args.live != "skip":
        catalog = getToolByName(portal, "portal_catalog")
        root = "/".join(portal.getPhysicalPath())
        paths = [b.getPath() for b in
                 catalog.unrestrictedSearchResults(path=root)]
        if args.limit:
            paths = paths[:args.limit]
        log.info("live: %d cataloged objects", len(paths))

        def work_live(path, stats):
            obj = portal.unrestrictedTraverse(path, None)
            if obj is not None:
                clean_annotations(obj, "stale", keep_ms, not args.commit,
                                  args.sizes, stats)

        run_batches(app, paths, work_live, "live", args, live_stats)

    if args.history != "skip":
        storage = getToolByName(portal, "portal_historiesstorage")
        repo = storage.zvc_repo
        hids = list(repo._histories.keys())
        if args.limit:
            hids = hids[:args.limit]
        log.info("history: %d histories", len(hids))
        hist_mode = "drop" if args.history == "drop" else "stale"

        def work_history(hid, stats):
            history = repo._histories[hid]
            for version in history._versions.values():
                wrapper = getattr(version, "_data", None)
                object_data = getattr(wrapper, "_object", None)
                obj = getattr(object_data, "object", None)
                if obj is not None:
                    stats["versions"] += 1
                    clean_annotations(obj, hist_mode, keep_ms,
                                      not args.commit, args.sizes, stats)

        run_batches(app, hids, work_history, "history", args, hist_stats)

    print(f"\n=== {mode} summary ===")
    for name, st in (("live", live_stats), ("history", hist_stats)):
        if not st and name == "live" and args.live == "skip":
            continue
        verb = "deleted" if args.commit else "would delete"
        print(f"{name}: objects with scales {st['objects_with_scales']:,}, "
              f"scales {st['scales']:,}, {verb} {st['deleted']:,} "
              f"in {st['objects_touched']:,} objects"
              + (f" ({human(st['deleted_bytes'])} declared)" if args.sizes
                 else "")
              + (f", versions visited {st['versions']:,}" if name == "history"
                 else "")
              + (f", FAILED batches {st['failed_batches']}"
                 if st["failed_batches"] else ""))
    if not args.commit:
        print("Nothing was changed. Re-run with --commit to apply.")


def script_args(argv):
    """Only the arguments after this script's name.

    Depending on how `bin/instance run` launches the script, sys.argv may
    also contain interpreter arguments (e.g. `-c scripts/cleanup_scales.py`).
    """
    for i in range(len(argv) - 1, -1, -1):
        if argv[i].endswith("cleanup_scales.py"):
            return argv[i + 1:]
    return argv[1:]


if "app" in globals():  # `app` is injected by `bin/instance run`
    main(app, script_args(sys.argv))  # noqa: F821
