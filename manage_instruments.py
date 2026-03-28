#!/usr/bin/env python3
"""Download or update SFZ instruments defined in a CSV manifest.

CSV columns (case-insensitive):
  name (required)        Human-friendly identifier; used for default dest.
  type (required)        Either 'git' or 'archive'.
  url (required)         Git remote or direct archive URL.
  dest (optional)        Destination relative to --dest-dir; defaults to a
                         slug of `name`.
  branch (optional)      Branch / tag / commit for git sources.
  git_subdir (optional)  For git only: sparse-checkout this subfolder. Use
                         'root' to keep only top-level files (no subdirs).
  enabled (optional)     If set to false/0/no, the row is skipped.

Example (see instruments.csv.example):
name,type,url,dest,branch
VSCO-Community,git,https://github.com/sgossner/VSCO-2-CE.git,VSCO-2-CE,main
SalamanderPiano,archive,https://example.com/salamander.tar.gz,SalamanderPiano
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def parse_bool(value: str) -> bool:
    value = value.strip().lower()
    return value in {"1", "true", "yes", "y"}


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "*/*",
}


def safe_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "instrument"


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


@dataclass
class Instrument:
    name: str
    kind: str
    url: str
    dest: Path
    branch: Optional[str] = None
    git_subdir: Optional[str] = None


def load_manifest(csv_path: Path, base_dir: Path) -> List[Instrument]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    instruments: List[Instrument] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"name", "type", "url"}
        missing = required - {c.lower() for c in reader.fieldnames or []}
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            if not row:
                continue
            normalized = {k.lower(): (v or "").strip() for k, v in row.items()}
            if not any(normalized.values()):
                continue
            if normalized.get("name", "").startswith("#"):
                continue
            if normalized.get("enabled") and not parse_bool(normalized["enabled"]):
                continue

            name = normalized["name"] or normalized["dest"]
            if not name:
                raise ValueError("Row is missing a name")

            kind = normalized["type"].lower()
            if kind not in {"git", "archive"}:
                raise ValueError(f"Unsupported type '{kind}' for {name}")

            url = normalized["url"]
            if not url:
                raise ValueError(f"Missing URL for {name}")

            git_subdir = normalized.get("git_subdir") or None
            if git_subdir and kind != "git":
                raise ValueError(f"git_subdir specified for non-git entry {name}")
            if git_subdir:
                git_subdir = validate_subpath(git_subdir)

            dest_name = normalized.get("dest") or safe_name(name)
            dest = (base_dir / dest_name).resolve()
            if not is_relative_to(dest, base_dir):
                raise ValueError(f"Destination for {name} escapes the base directory")

            instruments.append(
                Instrument(
                    name=name,
                    kind=kind,
                    url=url,
                    dest=dest,
                    branch=normalized.get("branch") or None,
                    git_subdir=git_subdir,
                )
            )
    return instruments


def log(msg: str) -> None:
    print(msg, flush=True)


def run_cmd(cmd: Iterable[str], cwd: Optional[Path], dry_run: bool) -> None:
    prefix = "[DRY]" if dry_run else "[RUN]"
    log(f"{prefix} {' '.join(cmd)}" + (f" (cwd={cwd})" if cwd else ""))
    if not dry_run:
        subprocess.check_call(list(cmd), cwd=str(cwd) if cwd else None)


def ensure_clean_destination(dest: Path, force: bool, dry_run: bool) -> None:
    if dest.exists():
        if not force:
            raise FileExistsError(
                f"Destination {dest} exists. Use --force to replace it."
            )
        log(f"[INFO] Removing existing destination {dest}")
        if not dry_run:
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()


def cache_file_path(cache_dir: Path, inst: Instrument, base_dir: Path) -> Path:
    rel = inst.dest.relative_to(base_dir)
    key = rel.as_posix().replace("/", "__")
    return cache_dir / f"{key}.json"


def load_cache(cache_dir: Path, inst: Instrument, base_dir: Path) -> Optional[Dict[str, str]]:
    path = cache_file_path(cache_dir, inst, base_dir)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("url") != inst.url:
            return None
        return data
    except Exception:
        return None


def save_cache(
    cache_dir: Path,
    inst: Instrument,
    base_dir: Path,
    url: str,
    etag: Optional[str],
    last_modified: Optional[str],
    content_length: Optional[str],
    sha256: Optional[str],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "url": url,
        "etag": etag,
        "last_modified": last_modified,
        "content_length": content_length,
        "sha256": sha256,
    }
    path = cache_file_path(cache_dir, inst, base_dir)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def fetch_remote_headers(url: str) -> Dict[str, str]:
    attempts = [
        urllib.request.Request(url, method="HEAD", headers=DEFAULT_HEADERS),
        urllib.request.Request(url, headers={"Range": "bytes=0-0", **DEFAULT_HEADERS}),
    ]
    for req in attempts:
        try:
            with urllib.request.urlopen(req) as resp:
                return dict(resp.headers.items())
        except Exception:
            continue
    return {}


def should_skip_download(headers: Dict[str, str], cache: Dict[str, str]) -> bool:
    if not cache:
        return False
    etag = headers.get("ETag")
    if etag and cache.get("etag") and etag == cache["etag"]:
        return True

    last_mod = headers.get("Last-Modified")
    content_len = headers.get("Content-Length")
    if (
        last_mod
        and content_len
        and cache.get("last_modified") == last_mod
        and cache.get("content_length") == content_len
    ):
        return True

    return False


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_subpath(path_str: str) -> str:
    path = Path(path_str.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or path_str.strip() == "":
        raise ValueError(f"Invalid subpath '{path_str}'")
    return str(path)


def configure_sparse_checkout(repo_path: Path, subdir: str, dry_run: bool) -> None:
    if subdir == "root":
        log(f"[INFO] Enabling sparse checkout for root files only in {repo_path}")
        run_cmd(["git", "sparse-checkout", "init", "--no-cone"], cwd=repo_path, dry_run=dry_run)
        # Include top-level files, exclude nested directories.
        run_cmd(["git", "sparse-checkout", "set", "/*", "!/*/*"], cwd=repo_path, dry_run=dry_run)
    else:
        log(f"[INFO] Enabling sparse checkout for '{subdir}' in {repo_path}")
        run_cmd(["git", "sparse-checkout", "init", "--cone"], cwd=repo_path, dry_run=dry_run)
        run_cmd(["git", "sparse-checkout", "set", subdir], cwd=repo_path, dry_run=dry_run)


def update_git(inst: Instrument, force: bool, dry_run: bool) -> None:
    dest = inst.dest
    git_dir = dest / ".git"

    if dest.exists() and not git_dir.is_dir():
        ensure_clean_destination(dest, force=force, dry_run=dry_run)

    if dest.exists() and git_dir.is_dir():
        log(f"[INFO] Updating git repo for {inst.name} at {dest}")
        run_cmd(["git", "fetch", "--all", "--prune", "--prune-tags"], cwd=dest, dry_run=dry_run)
        if inst.branch:
            run_cmd(["git", "checkout", inst.branch], cwd=dest, dry_run=dry_run)
        run_cmd(["git", "pull", "--ff-only"], cwd=dest, dry_run=dry_run)
    else:
        log(f"[INFO] Cloning git repo for {inst.name} into {dest}")
        clone_cmd = ["git", "clone"]
        if inst.branch:
            clone_cmd += ["--branch", inst.branch]
        clone_cmd += [inst.url, str(dest)]
        run_cmd(clone_cmd, cwd=None, dry_run=dry_run)

    if inst.git_subdir:
        configure_sparse_checkout(dest, inst.git_subdir, dry_run=dry_run)


def download_file(url: str, target: Path, dry_run: bool) -> Dict[str, str]:
    log(f"[INFO] Downloading {url} -> {target}")
    if dry_run:
        return {}

    req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(req) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
        return dict(response.headers.items())


def extract_archive(archive_path: Path, extract_to: Path) -> None:
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_to)
    elif tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as tf:
            tf.extractall(extract_to)
    else:
        raise ValueError(f"Unsupported archive format: {archive_path}")


def find_top_level(extract_dir: Path) -> Optional[Path]:
    entries = [p for p in extract_dir.iterdir() if not p.name.startswith("__MACOSX")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return None


def merge_directory(source: Path, dest: Path, dry_run: bool) -> None:
    """Merge source into dest, overwriting files with the same path."""
    log(f"[INFO] Merging archive contents into {dest}")
    if dry_run:
        return
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = dest / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def update_archive(
    inst: Instrument,
    force: bool,
    dry_run: bool,
    temp_dir: Path,
    cache_dir: Path,
    base_dir: Path,
    force_download: bool,
) -> None:
    cache = load_cache(cache_dir, inst, base_dir)
    headers: Dict[str, str] = {}
    if not force_download:
        headers = fetch_remote_headers(inst.url)
        if cache and should_skip_download(headers, cache):
            log(f"[INFO] Skipping download for {inst.name}; remote unchanged.")
            return

    with tempfile.TemporaryDirectory(dir=temp_dir) as workdir_str:
        workdir = Path(workdir_str)
        archive_path = workdir / "download.bin"
        download_headers = download_file(inst.url, archive_path, dry_run=dry_run)
        extract_dir = workdir / "extracted"
        if not dry_run:
            extract_dir.mkdir(parents=True, exist_ok=True)
            extract_archive(archive_path, extract_dir)

        source = find_top_level(extract_dir) or extract_dir

        # Archive installs merge into the destination; files with the same path
        # are overwritten. This allows multiple archives to share a target path.
        merge_directory(source, inst.dest, dry_run=dry_run)

        if not dry_run:
            etag = download_headers.get("ETag") or headers.get("ETag")
            last_modified = download_headers.get("Last-Modified") or headers.get("Last-Modified")
            content_length = download_headers.get("Content-Length") or headers.get("Content-Length")
            sha256 = hash_file(archive_path)
            save_cache(cache_dir, inst, base_dir, inst.url, etag, last_modified, content_length, sha256)


def process_instrument(
    inst: Instrument,
    force: bool,
    dry_run: bool,
    temp_dir: Path,
    cache_dir: Path,
    base_dir: Path,
    force_download: bool,
) -> None:
    if inst.kind == "git":
        update_git(inst, force=force, dry_run=dry_run)
    elif inst.kind == "archive":
        update_archive(
            inst,
            force=force,
            dry_run=dry_run,
            temp_dir=temp_dir,
            cache_dir=cache_dir,
            base_dir=base_dir,
            force_download=force_download,
        )
    else:
        raise ValueError(f"Unknown type {inst.kind}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download/update SFZ instruments from CSV manifest.")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("instruments.csv"),
        help="Path to CSV manifest (default: instruments.csv).",
    )
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=Path("."),
        help="Base destination directory for instruments.",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=None,
        help="Directory for temporary files (default: system temp).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing destinations for git; archives always overwrite.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Always download archives even if metadata suggests unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions without downloading or modifying files.",
    )

    args = parser.parse_args(argv)
    base_dir = args.dest_dir.expanduser().resolve()
    temp_dir = args.temp_dir.expanduser().resolve() if args.temp_dir else None

    if not base_dir.exists():
        log(f"[INFO] Creating destination directory {base_dir}")
        if not args.dry_run:
            base_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = base_dir / ".instrument_cache"

    try:
        instruments = load_manifest(args.csv.expanduser().resolve(), base_dir)
    except Exception as exc:  # pragma: no cover - user feedback path
        log(f"[ERROR] {exc}")
        return 1

    if not instruments:
        log("[WARN] No instruments defined in the manifest.")
        return 0

    for inst in instruments:
        log(f"[INFO] Processing {inst.name} ({inst.kind})")
        try:
            process_instrument(
                inst,
                force=args.force,
                dry_run=args.dry_run,
                temp_dir=temp_dir or Path(tempfile.gettempdir()),
                cache_dir=cache_dir,
                base_dir=base_dir,
                force_download=args.force_download,
            )
        except Exception as exc:  # pragma: no cover - user feedback path
            log(f"[ERROR] {inst.name}: {exc}")
            return 1

    log("[INFO] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

