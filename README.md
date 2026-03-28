## SFZ Instrument Manager

Utility script to download and update SFZ instruments from a CSV manifest. Supports git repos (with optional sparse checkout) and direct archive downloads with caching and automatic merging.

### Files
- `manage_instruments.py` – CLI tool.
- `instruments.csv.example` – sample manifest; copy to `instruments.csv` and edit.

### CSV columns (case-insensitive)
- `name` (required) – label for logs; also used for default dest name.
- `type` (required) – `git` or `archive`.
- `url` (required) – git remote or archive URL.
- `dest` (optional) – destination relative to `--dest-dir`; defaults to a slug of `name`.
- `branch` (optional, git) – branch / tag / commit.
- `git_subdir` (optional, git) – sparse checkout path; use `root` to keep only top-level files.
- `enabled` (optional) – if set to false/0/no, row is skipped.
- `archive_subdir` – deprecated/ignored; archive root is always used.

### Behavior
- Git: clone if missing; otherwise fetch/pull; respects `branch`; sparse-checkout when `git_subdir` set.
- Archive: HEAD/Range pre-check with cached ETag/Last-Modified/Content-Length; skips download when unchanged unless `--force-download` is set. Archives always merge into the destination; files with the same path are overwritten (allows multiple archives to share a target).
- Cache: stored in `--dest-dir/.instrument_cache` (per dest path key) with URL and archive hash/headers.
- Dry run: logs actions without downloading or writing.

### Usage
```bash
# edit your manifest first
cp instruments.csv.example instruments.csv
# run (downloads into current directory)
python3 manage_instruments.py --csv instruments.csv

# set a destination root explicitly
python3 manage_instruments.py --csv instruments.csv --dest-dir "/mnt/files/Documents/SFZ"

# force archive download even if headers match
python3 manage_instruments.py --force-download

# dry run to preview actions
python3 manage_instruments.py --dry-run
```

### Notes
- Archives no longer need `archive_subdir`; the archive root (or sole top-level folder) is used.
- `--force` only applies to git destinations; archive installs always overwrite/merge.
- If a server blocks HEAD, the script falls back to a tiny Range request and uses a browser-like User-Agent to reduce 406 responses.
