#!/usr/bin/env python3
"""Normalize source-distribution tar metadata before publishing.

Setuptools writes the local account name and group into sdist headers.  This
rewrites only tar ownership fields, leaving package contents unchanged, so a
release cannot disclose the build host account.
"""

from __future__ import annotations

import argparse
import gzip
import os
import tarfile
import tempfile
from pathlib import Path


def _safe_member(name: str) -> None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe archive member path: {name!r}")


def normalize(path: Path) -> None:
    if path.suffixes[-2:] != [".tar", ".gz"]:
        raise ValueError(f"expected a .tar.gz source distribution: {path}")

    directory = path.parent
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with tarfile.open(path, mode="r:gz") as source, temporary.open("wb") as raw:
            compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
            with compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as target:
                    for member in source:
                        _safe_member(member.name)
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        if member.isfile():
                            fileobj = source.extractfile(member)
                            target.addfile(member, fileobj)
                        else:
                            target.addfile(member)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path, help=".tar.gz sdists to normalize")
    args = parser.parse_args()
    for archive in args.archives:
        normalize(archive)
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
