import argparse, datetime, exiftool, os, shutil, sqlite3, sys, threading, time, json, zlib
from pathlib import Path
from datetime import date
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import multiprocessing

DEFAULT_CACHE_DB = Path.home() / '.cache' / 'exifbuddy' / 'dedupe.db'


class HashCache:
    """SQLite-backed cache for file CRC32 hashes, keyed by absolute path.

    A cache entry is valid only when the file's current (size, mtime) matches
    the recorded values — same invariant rsync uses.
    """

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._pending = []  # buffered writes, flushed in batches
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS crc_cache (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    crc32 TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_size ON crc_cache(size);
            """)
            self._conn.commit()

    def load_known(self):
        """Return {path: (size, mtime, crc)} for all rows."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, size, mtime, crc32 FROM crc_cache"
            ).fetchall()
        return {p: (s, m, c) for p, s, m, c in rows}

    def stash(self, path, size, mtime, crc):
        with self._lock:
            self._pending.append((path, size, mtime, crc, time.time()))
            if len(self._pending) >= 500:
                self._flush_locked()

    def _flush_locked(self):
        if not self._pending:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO crc_cache(path, size, mtime, crc32, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            self._pending,
        )
        self._conn.commit()
        self._pending = []

    def flush(self):
        with self._lock:
            self._flush_locked()

    def stats(self):
        with self._lock:
            (n,) = self._conn.execute("SELECT COUNT(*) FROM crc_cache").fetchone()
        return n

    def close(self):
        self.flush()
        with self._lock:
            self._conn.close()


DATE_FIELDS = [
    'EXIF:DateTimeOriginal',
    'EXIF:CreateDate',
    'EXIF:ModifyDate',
    'File:FileModifyDate',
    'QuickTime:CreateDate',
    'QuickTime:CreationDate',
]


def extract_date(source_file):
    """Return (date_str, source) — source is 'exif', 'fs', or 'none'."""
    for field in DATE_FIELDS:
        if field in source_file:
            try:
                s = source_file[field].split('+')[0].split('-')[0].strip()
                d = datetime.datetime.strptime(s, '%Y:%m:%d %H:%M:%S')
                return d.strftime('%Y %b %d'), 'exif'
            except Exception:
                continue
    try:
        ctime = os.path.getctime(source_file['SourceFile'])
        return datetime.datetime.fromtimestamp(ctime).strftime('%Y %b %d'), 'fs'
    except Exception:
        return None, 'none'


def generate_target_dictionary(metadata, no_date_bucket=None):
    """Build the copy plan.

    no_date_bucket: if set, files with no EXIF date are routed to a folder of
    that name with the original filename unchanged (no date prefix). If None,
    they fall back to filesystem ctime (legacy behavior).
    """
    copylist = {}
    stats = Counter()

    for source_file in metadata:
        key = str(source_file['SourceFile'])
        file = Path(key).name
        containing_folder = Path(key).parent.name

        date_str, date_source = extract_date(source_file)
        stats[date_source] += 1

        if date_source == 'exif':
            subdir = f'{date_str} - {containing_folder}'
            dest_file = f'{date_str} - {file}'
        elif no_date_bucket and date_source != 'exif':
            # Explicit no-date bucket: don't fake a date, group by original folder.
            subdir = f'{no_date_bucket}/{containing_folder}'
            dest_file = file
        elif date_source == 'fs':
            subdir = f'{date_str} - {containing_folder}'
            dest_file = f'{date_str} - {file}'
        else:
            subdir = no_date_bucket or '_no_date'
            dest_file = file

        copylist[key] = {
            'source': key,
            'containing_folder': subdir,
            'dest_file': dest_file,
            'date_source': date_source,
        }

    return copylist, stats


def search_files_in_path(search_path, extensions):
    """Recursively find files whose suffix matches one of `extensions`.

    `extensions` is an iterable of lowercase extensions without a leading dot
    (e.g. ['jpg', 'png', 'heic']).
    """
    wanted = {f'.{e.lower().lstrip(".")}' for e in extensions}
    discovered_files = []
    ext_counts = Counter()
    posix_path = Path(search_path)

    for glob_file in posix_path.rglob('*'):
        if glob_file.name.startswith('._'):
            continue
        if not glob_file.is_file():
            continue
        ext = glob_file.suffix.lower()
        if ext in wanted:
            discovered_files.append(str(glob_file))
            ext_counts[ext] += 1

    return discovered_files, ext_counts


def process_copylist(copylist, destination, mode='copy'):
    max_workers = min(multiprocessing.cpu_count() * 4, 32)
    total = len(copylist)
    counts = Counter()

    def tally(result):
        counts[result] += 1
        n = sum(counts.values())
        if n % 100 == 0:
            print(f'  ... {n}/{total}  ('
                  f'new={counts["new"]} skipped={counts["skipped"]} '
                  f'renamed={counts["renamed"]} errors={counts["error"]})')

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(write_new_files, copylist[k], destination, mode) for k in copylist]
        for future in futures:
            try:
                tally(future.result())
            except Exception as e:
                counts['error'] += 1
                print(f'Error processing file: {e}')

    print()
    print(f'Done. new={counts["new"]}  skipped={counts["skipped"]}  '
          f'renamed-due-to-collision={counts["renamed"]}  errors={counts["error"]}')


def write_new_files(items, destination, mode):
    """Returns 'new' (wrote a new file), 'skipped' (identical target already
    there), or 'renamed' (same name existed but different content, used -N suffix).
    """
    source = items['source']
    new_file = items['dest_file']
    subdir = items['containing_folder']

    output_path = Path(destination) / subdir
    output_path.mkdir(parents=True, exist_ok=True)

    target = output_path / new_file
    result = 'new'

    if target.exists():
        try:
            src_size = os.path.getsize(source)
            tgt_size = os.path.getsize(target)
        except OSError:
            src_size = tgt_size = None

        if src_size is not None and src_size == tgt_size:
            # Treat same-size as same-file: idempotent skip.
            # In move mode, still drop the source so move semantics hold.
            if mode == 'move':
                try:
                    os.remove(source)
                except OSError:
                    pass
            return 'skipped'

        # Genuine collision (same name, different content) — fall through to -N
        stem = target.stem
        suffix = target.suffix
        n = 2
        while True:
            candidate = output_path / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                target = candidate
                break
            n += 1
        result = 'renamed'

    if mode == 'move':
        shutil.move(source, target)
    else:
        shutil.copy(source, target)
    return result

def bytes_human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def _crc32_of(path):
    crc = 0
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                crc = zlib.crc32(chunk, crc)
        return f'{crc & 0xFFFFFFFF:08x}'
    except OSError:
        return None


def find_duplicate_groups(root, extensions=None, cache=None):
    """Walk `root`, return list of (size, crc, [paths]) for groups with len>=2.

    Pass 1: bucket files by size (cheap stat).
    Pass 2: hash only the size-clash groups; if `cache` is provided, look up
    each (path, size, mtime) and reuse the cached CRC32 on hit. Newly-computed
    hashes are written back to the cache.
    """
    root = Path(root)
    wanted = None
    if extensions:
        wanted = {f'.{e.lower().lstrip(".")}' for e in extensions}

    # Pass 1: size buckets + record mtimes for cache lookup
    by_size = defaultdict(list)
    mtime_of = {}
    scanned = 0
    for p in root.rglob('*'):
        if p.name.startswith('._') or not p.is_file():
            continue
        if wanted is not None and p.suffix.lower() not in wanted:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size == 0:
            continue
        sp = str(p)
        by_size[st.st_size].append(sp)
        mtime_of[sp] = st.st_mtime
        scanned += 1
        if scanned % 5000 == 0:
            sys.stdout.write(f'\r  scanned {scanned} files...')
            sys.stdout.flush()
    sys.stdout.write('\r' + ' ' * 60 + '\r')
    print(f'Scanned {scanned} files. Size buckets with >=2 files: '
          f'{sum(1 for v in by_size.values() if len(v) >= 2)}')

    # Pass 2: hash size-clash groups
    suspects = [(size, paths) for size, paths in by_size.items() if len(paths) >= 2]
    total_to_hash = sum(len(p) for _, p in suspects)
    if total_to_hash == 0:
        return []

    # Consult cache for hits first
    cache_hits = {}  # path -> crc
    if cache is not None:
        known = cache.load_known()
        print(f'Cache: {len(known)} entries loaded from {cache.db_path}')
        for cur_size, paths in suspects:
            for p in paths:
                rec = known.get(p)
                if rec is None:
                    continue
                csize, cmtime, ccrc = rec
                if cur_size == csize and abs(cmtime - mtime_of[p]) < 1e-6:
                    cache_hits[p] = ccrc
        print(f'Cache hits: {len(cache_hits)}/{total_to_hash}  '
              f'({len(cache_hits) * 100 // max(total_to_hash, 1)}%)')

    to_compute = total_to_hash - len(cache_hits)
    if to_compute:
        print(f'Hashing {to_compute} files (CRC32)...')
    else:
        print('All hashes served from cache — no I/O needed.')

    groups_by_key = defaultdict(list)
    hashed = 0
    max_workers = min(multiprocessing.cpu_count() * 2, 16)

    def hash_or_cache(path):
        cached = cache_hits.get(path)
        if cached is not None:
            return path, cached, True
        crc = _crc32_of(path)
        return path, crc, False

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for size, paths in suspects:
            futures = {ex.submit(hash_or_cache, p): p for p in paths}
            for fut in futures:
                path, crc, from_cache = fut.result()
                if not from_cache:
                    hashed += 1
                    if cache is not None and crc is not None:
                        cache.stash(path, size, mtime_of[path], crc)
                    if hashed % 500 == 0:
                        sys.stdout.write(f'\r  hashed {hashed}/{to_compute}...')
                        sys.stdout.flush()
                if crc is None:
                    continue
                groups_by_key[(size, crc)].append(path)
    sys.stdout.write('\r' + ' ' * 60 + '\r')
    if cache is not None:
        cache.flush()

    groups = [(size, crc, paths) for (size, crc), paths in groups_by_key.items()
              if len(paths) >= 2]
    return groups


def _select_keeper(paths, strategy):
    if strategy == 'first':
        return paths[0]
    if strategy == 'alpha':
        return sorted(paths)[0]
    if strategy == 'shortest':
        return min(paths, key=lambda p: (len(p), p))
    # 'oldest' (default): smallest mtime; alphabetical first as tiebreak so the
    # result is deterministic when two files were written in the same second.
    def key(p):
        try:
            return (os.path.getmtime(p), p)
        except OSError:
            return (float('inf'), p)
    return min(paths, key=key)


def quarantine_dupes(groups, quarantine_dir, scan_root, keep_strategy='shortest', dry_run=True):
    """For each duplicate group, keep one file and move the rest into
    `quarantine_dir`, preserving the relative path from `scan_root`.
    """
    scan_root = Path(scan_root).resolve()
    quarantine_dir = Path(quarantine_dir)

    plan = []  # (src, dest, group_size, group_crc)
    total_bytes = 0
    for size, crc, paths in groups:
        keeper = _select_keeper(paths, keep_strategy)
        for p in paths:
            if p == keeper:
                continue
            rel = Path(p).resolve().relative_to(scan_root)
            dest = quarantine_dir / rel
            plan.append((p, str(dest), size, crc, keeper))
            total_bytes += size

    print(f'Duplicate groups: {len(groups)}')
    print(f'Files to quarantine: {len(plan)}  ({bytes_human(total_bytes)} reclaimable)')
    print(f'Keeper strategy: {keep_strategy}')
    print()

    if not plan:
        return

    # Sample
    print('Sample (first 10 groups by reclaimable bytes):')
    sample_groups = sorted(groups, key=lambda g: g[0] * (len(g[2]) - 1), reverse=True)[:10]
    for size, crc, paths in sample_groups:
        keeper = _select_keeper(paths, keep_strategy)
        try:
            kept_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(keeper)).strftime('%Y-%m-%d %H:%M')
        except OSError:
            kept_mtime = '?'
        print(f'  [{crc} {bytes_human(size)} x{len(paths)-1} dupes]')
        print(f'    KEEP ({kept_mtime}):  {keeper}')
        for p in paths:
            if p != keeper:
                try:
                    mt = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime('%Y-%m-%d %H:%M')
                except OSError:
                    mt = '?'
                print(f'    MOVE ({mt}):  {p}')
    print()

    if dry_run:
        print(f'DRY RUN — no files moved. Re-run without --dry-run to apply.')
        return

    print(f'Moving {len(plan)} files into {quarantine_dir}...')
    moved = 0
    failed = 0
    for src, dest, _size, _crc, _keeper in plan:
        try:
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            # If dest already exists (prior quarantine run), append -N
            target = Path(dest)
            if target.exists():
                stem, suffix = target.stem, target.suffix
                n = 2
                while True:
                    candidate = target.with_name(f'{stem}-{n}{suffix}')
                    if not candidate.exists():
                        target = candidate
                        break
                    n += 1
            shutil.move(src, target)
            moved += 1
            if moved % 100 == 0:
                print(f'  ... {moved}/{len(plan)}')
        except Exception as e:
            failed += 1
            print(f'  FAIL: {src} -> {dest}: {e}')
    print(f'Done. Moved {moved}, failed {failed}.')


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description='Sort photos/videos into YYYY MMM DD - <folder>/ buckets by EXIF date, '
                    'or find/quarantine duplicate files in a folder.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument('-i', '--ifile', dest='search_path',
                    help='Input directory to search recursively (sort mode)')
    ap.add_argument('-o', '--ofile', dest='destination',
                    help='Output directory (sort mode)')
    ap.add_argument('--dedupe-in', dest='dedupe_in', metavar='PATH',
                    help='Switch to dedupe mode: find duplicate files inside this folder.')
    ap.add_argument('--dupes-to', dest='dupes_to', metavar='DIR',
                    help='Quarantine destination for duplicates (dedupe mode). '
                         'Required for non-dry-run dedupe.')
    ap.add_argument('--keep', choices=['oldest', 'shortest', 'first', 'alpha'], default='oldest',
                    help='Which file in a duplicate group to keep (dedupe mode). '
                         "'oldest' compares filesystem mtime.")
    ap.add_argument('--cache-db', dest='cache_db', default=str(DEFAULT_CACHE_DB),
                    help='SQLite cache for CRC32 hashes (dedupe mode). '
                         'Set to empty string to disable.')
    ap.add_argument('--extensions',
                    default='jpg,jpeg,png,heic,tif,tiff,cr2,nef,arw,mp4,mov,mpo',
                    help='Comma-separated extensions to include (without dots)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print plan summary without copying/moving')
    ap.add_argument('--no-date-bucket', default=None, metavar='NAME',
                    help="Folder name for files with no EXIF date. If unset, fall back to filesystem ctime.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--copy', dest='mode', action='store_const', const='copy', default='copy',
                     help='Copy files (default)')
    mode.add_argument('--move', dest='mode', action='store_const', const='move',
                     help='Move files instead of copying')
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    extensions = [e.strip() for e in args.extensions.split(',') if e.strip()]

    # Dedupe mode
    if args.dedupe_in:
        if not args.dry_run and not args.dupes_to:
            sys.exit('--dupes-to DIR is required unless --dry-run')
        cache = HashCache(args.cache_db) if args.cache_db else None
        print(f'Dedupe mode')
        print(f'Scanning:    {args.dedupe_in}')
        print(f'Extensions:  {", ".join(extensions)}')
        if args.dupes_to:
            print(f'Quarantine:  {args.dupes_to}')
        print(f'Keep:        {args.keep}' + ('  [DRY RUN]' if args.dry_run else ''))
        if cache is not None:
            print(f'Cache DB:    {cache.db_path}')
        print()
        try:
            groups = find_duplicate_groups(args.dedupe_in, extensions=extensions, cache=cache)
            quarantine_dupes(
                groups,
                quarantine_dir=args.dupes_to or '/dev/null',
                scan_root=args.dedupe_in,
                keep_strategy=args.keep,
                dry_run=args.dry_run,
            )
        finally:
            if cache is not None:
                cache.close()
        return

    # Sort mode (default) — requires -i/-o
    if not args.search_path or not args.destination:
        sys.exit('Sort mode requires -i and -o. (Or use --dedupe-in for dedupe mode.)')

    print(f'Input path:  {args.search_path}')
    print(f'Output path: {args.destination}')
    print(f'Extensions:  {", ".join(extensions)}')
    print(f'Mode:        {args.mode}' + ('  [DRY RUN]' if args.dry_run else ''))
    if args.no_date_bucket:
        print(f'No-date bucket: {args.no_date_bucket}')
    print()

    lines, ext_counts = search_files_in_path(args.search_path, extensions)
    print(f'Files found: {len(lines)}')
    for ext, n in sorted(ext_counts.items(), key=lambda x: -x[1]):
        print(f'  {ext:>8}: {n}')
    print()

    if not lines:
        print('Nothing to do.')
        return

    with exiftool.ExifToolHelper() as et:
        metadata = []
        batch_size = 50
        for i in range(0, len(lines), batch_size):
            batch = lines[i:i + batch_size]
            try:
                metadata.extend(et.get_metadata(batch))
            except Exception as e:
                print(f'Error processing batch: {e}')
                for f in batch:
                    try:
                        metadata.append(et.get_metadata([f])[0])
                    except Exception as e2:
                        print(f'Could not read metadata for {f}: {e2}')
                        metadata.append({'SourceFile': f})
            if (i // batch_size + 1) % 20 == 0:
                print(f'  ... metadata: {min(i + batch_size, len(lines))}/{len(lines)}')

    if not metadata:
        print('No metadata extracted.')
        return

    copylist, stats = generate_target_dictionary(metadata, no_date_bucket=args.no_date_bucket)

    print()
    print('Date sources:')
    print(f'  exif: {stats["exif"]}')
    print(f'  fs ctime fallback: {stats["fs"]}')
    print(f'  none: {stats["none"]}')
    print()

    if args.dry_run:
        # Show a sample plan and per-bucket counts
        bucket_counts = Counter(v['containing_folder'] for v in copylist.values())
        print(f'Output buckets: {len(bucket_counts)}')
        print('Top 15 buckets by file count:')
        for bucket, n in bucket_counts.most_common(15):
            print(f'  {n:>6}  {bucket}')
        print()
        print('Sample of planned destinations (first 10):')
        for i, (_, v) in enumerate(copylist.items()):
            if i >= 10:
                break
            print(f"  [{v['date_source']:>4}] {v['containing_folder']}/{v['dest_file']}")
        print()
        print(f'DRY RUN — nothing was {args.mode}-ed. Re-run without --dry-run to apply.')
        return

    process_copylist(copylist, args.destination, mode=args.mode)
    print(f'Done. {len(copylist)} files {args.mode}-ed.')


if __name__ == '__main__':
    main(sys.argv[1:])