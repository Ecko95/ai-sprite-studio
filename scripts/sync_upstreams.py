#!/usr/bin/env python3
"""Verify or refresh the vendored upstream asset snapshot."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "upstream-lock.json"
ASSET_ROOT = ROOT / "src/ai_sprite_studio/assets/upstream"


def _load_lock() -> dict:
    lock = json.loads(LOCK_PATH.read_text())
    if lock.get("version") != 1 or not isinstance(lock.get("entries"), list):
        raise ValueError("unsupported upstream lock format")
    return lock


def _contained(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"path escapes its root: {relative}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _destination(relative: str) -> Path:
    destination = _contained(ROOT, relative)
    if not destination.is_relative_to(ASSET_ROOT.resolve()):
        raise ValueError(f"destination outside upstream asset root: {relative}")
    return destination


def _check(lock: dict) -> list[str]:
    errors = []
    seen = set()
    for entry in lock["entries"]:
        destination = entry["destination_path"]
        if destination in seen:
            errors.append(f"duplicate destination: {destination}")
            continue
        seen.add(destination)
        path = _destination(destination)
        if not path.is_file():
            errors.append(f"missing: {destination}")
        elif _sha256(path) != entry["sha256"]:
            errors.append(f"hash mismatch: {destination}")
    return errors


def _checkout(checkout_root: Path, entry: dict) -> Path:
    slug = entry["repository_url"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return _contained(checkout_root, slug)


def _verify_checkouts(checkout_root: Path, entries: list[dict]) -> None:
    repositories = {}
    for entry in entries:
        repositories.setdefault(entry["repository_url"], []).append(entry)

    for repo_entries in repositories.values():
        checkout = _checkout(checkout_root, repo_entries[0])
        revisions = {entry["revision"] for entry in repo_entries}
        if len(revisions) != 1:
            raise ValueError(f"multiple revisions for {repo_entries[0]['repository_url']}")
        expected_revision = revisions.pop()
        result = subprocess.run(
            ["git", "-C", checkout, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() != expected_revision:
            raise ValueError(f"checkout revision mismatch: {checkout}")
        status = subprocess.run(
            [
                "git",
                "-C",
                checkout,
                "status",
                "--porcelain",
                "--",
                *(entry["source_path"] for entry in repo_entries),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if status.stdout:
            raise ValueError(f"locked source files are modified: {checkout}")


def _update(lock: dict, checkout_root: Path) -> None:
    checkout_root = checkout_root.resolve()
    destinations = [_destination(entry["destination_path"]) for entry in lock["entries"]]
    _verify_checkouts(checkout_root, lock["entries"])
    for entry, destination in zip(lock["entries"], destinations, strict=True):
        source = _contained(_checkout(checkout_root, entry), entry["source_path"])
        if not source.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        entry["sha256"] = _sha256(destination)
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="verify the local snapshot")
    modes.add_argument("--update", type=Path, metavar="CHECKOUT_ROOT", help="refresh from checkouts")
    args = parser.parse_args()

    try:
        lock = _load_lock()
        if args.check:
            errors = _check(lock)
            if errors:
                for error in errors:
                    print(error, file=sys.stderr)
                return 1
            print(f"Verified {len(lock['entries'])} upstream assets.")
        else:
            _update(lock, args.update)
            print(f"Updated {len(lock['entries'])} upstream assets.")
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print(f"sync_upstreams: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
