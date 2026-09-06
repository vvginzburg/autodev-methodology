#!/usr/bin/env python3
"""Materialize pinned, unchanged skill sources without installing global hooks."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile


def digest(data):
    return hashlib.sha256(data).hexdigest()


def entries(archive):
    result = {}
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts:
                raise ValueError('Unsafe archive path: ' + member.name)
            name = str(path)
            if name in result:
                raise ValueError('Duplicate archive entry: ' + name)
            if member.isdir():
                kind, data = 'dir', b''
            elif member.isfile():
                kind, data = 'file', tar.extractfile(member).read()
            elif member.issym():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or '..' in target.parts:
                    raise ValueError('Unsafe symlink: ' + name)
                kind, data = 'symlink', member.linkname.encode()
            else:
                raise ValueError('Unsupported archive entry: ' + name)
            result[name] = (kind, member.mode & 0o777, data)
    for name, (kind, mode, data) in result.items():
        for parent in PurePosixPath(name).parents:
            if str(parent) in result and result[str(parent)][0] != 'dir':
                raise ValueError('Non-directory ancestor: ' + name)
        if kind == 'symlink':
            target = str(PurePosixPath(name).parent / data.decode())
            if target not in result or result[target][0] != 'file':
                raise ValueError('Symlink must target a packaged regular file: ' + name)
    return result


def verify(folder, expected):
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Package root must be a real directory: ' + str(folder))
    actual = set()
    for parent, dirs, files in os.walk(folder, followlinks=False):
        for child in dirs + files:
            actual.add((Path(parent) / child).relative_to(folder).as_posix())
    if actual != set(expected):
        raise ValueError('Existing source tree has missing or extra entries: ' + str(folder))
    for name, (kind, mode, data) in expected.items():
        path = folder / name
        info = path.lstat()
        if kind == 'symlink':
            ok = stat.S_ISLNK(info.st_mode) and os.readlink(path).encode() == data
        elif kind == 'dir':
            ok = stat.S_ISDIR(info.st_mode) and not path.is_symlink()
        else:
            ok = stat.S_ISREG(info.st_mode) and path.read_bytes() == data
        if not ok or (kind != 'symlink' and stat.S_IMODE(info.st_mode) != mode):
            raise ValueError('Existing source changed: ' + str(path))


def prepare(destination, lock_path):
    destination = Path(destination).absolute()
    lock_path = Path(lock_path).resolve()
    lock = json.loads(lock_path.read_text())
    prepared = []
    names = set()
    for package in lock['packages']:
        name = package['name']
        if (not name or name in ('.', '..') or '/' in name or '\\' in name
                or not all(c.isascii() and (c.isalnum() or c in '-_') for c in name)
                or name in names):
            raise ValueError('Unsafe or duplicate package name')
        names.add(name)
        if Path(package['archive']).name != package['archive']:
            raise ValueError('Archive must be adjacent to lock file')
        archive = lock_path.parent / package['archive']
        if digest(archive.read_bytes()) != package['sha256']:
            raise ValueError('Archive hash mismatch: ' + str(archive))
        prepared.append((package, entries(archive)))
    # Existing installations are read-only verified, never repaired in place.
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError('Destination must be a real directory')
        if set(p.name for p in destination.iterdir()) != set(p['name'] for p, _ in prepared):
            raise ValueError('Destination has unexpected entries')
        for package, contents in prepared:
            verify(destination / package['name'], contents)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix='.autodev-sources-', dir=destination.parent))
        try:
            for package, contents in prepared:
                base = temp / package['name']
                base.mkdir()
                for name, (kind, mode, data) in contents.items():
                    path = base / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if kind == 'dir':
                        path.mkdir(exist_ok=True)
                    elif kind == 'file':
                        path.write_bytes(data)
                    else:
                        path.symlink_to(data.decode())
                    if kind != 'symlink':
                        path.chmod(mode)
                verify(base, contents)
            if destination.exists() or destination.is_symlink():
                raise ValueError('Destination appeared during preparation')
            temp.rename(destination)
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    return [{'name': p['name'], 'version': p['version'], 'commit': p['commit'],
             'path': str(destination / p['name']), 'archive_sha256': p['sha256']}
            for p, _ in prepared]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', required=True)
    parser.add_argument('--lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'dependencies/lock.json')
    args = parser.parse_args()
    print(json.dumps(prepare(args.dest, args.lock), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
