#!/usr/bin/env python3
"""Capture a bounded working-tree candidate before commit, without changing Git."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env)


def safe_path(value):
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('Scope requires explicit relative paths, not root or .git')
    return str(path)


def scope_config(raw):
    paths = sorted(set(safe_path(p) for p in raw['paths']))
    if not paths:
        raise ValueError('Empty scope')
    excluded = []
    for item in raw.get('exclude', []):
        if not item.get('reason', '').strip():
            raise ValueError('Every exclusion requires a reason')
        excluded.append({'path': safe_path(item['path']), 'reason': item['reason']})
    return {'paths': paths, 'exclude': excluded}


def inside(path, roots):
    return any(path == root or path.startswith(root + '/') for root in roots)


def selected(name, scope):
    return inside(name, scope['paths']) and not inside(name, [e['path'] for e in scope['exclude']])


def record(mode, data, objects):
    key = sha(data)
    objects[key] = data
    return {'mode': mode, 'sha256': key, 'bytes': len(data)}


def working(repo, scope, objects):
    result = {}
    def visit(path):
        name = path.relative_to(repo).as_posix()
        if not selected(name, scope):
            return
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):
                if child.name == '.git':
                    raise ValueError('Nested repository in scope: ' + name)
                visit(child)
        elif stat.S_ISLNK(info.st_mode):
            result[name] = record('120000', os.fsencode(os.readlink(path)), objects)
        elif stat.S_ISREG(info.st_mode):
            mode = '100755' if info.st_mode & 0o111 else '100644'
            result[name] = record(mode, path.read_bytes(), objects)
        else:
            raise ValueError('Unsupported file type: ' + name)
    for root in scope['paths']:
        # Never traverse a symlink ancestor of an explicitly selected file.
        for parent in PurePosixPath(root).parents:
            if str(parent) != '.' and (repo / str(parent)).is_symlink():
                raise ValueError('Symlink ancestor in scope: ' + root)
        visit(repo / root)
    return result


def baseline(repo, commit, scope, objects):
    result = {}
    for row in git(repo, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        path = os.fsdecode(name)
        if selected(path, scope):
            if kind != 'blob':
                raise ValueError('Submodule in scope: ' + path)
            result[path] = record(mode, git(repo, 'cat-file', 'blob', oid), objects)
    return result


def index_entries(repo, scope, objects):
    result = {}
    for row in git(repo, 'ls-files', '--stage', '-z').split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        mode, oid, stage = meta.decode().split()
        path = os.fsdecode(name)
        if selected(path, scope):
            if stage != '0' or mode == '160000':
                raise ValueError('Unmerged file or submodule: ' + path)
            result[path] = record(mode, git(repo, 'cat-file', 'blob', oid), objects)
    return result


def index_hash(repo):
    path = Path(os.fsdecode(git(repo, 'rev-parse', '--git-path', 'index')).strip())
    if not path.is_absolute():
        path = repo / path
    return sha(path.read_bytes()) if path.exists() else None


def identity(manifest):
    keys = ('baseline', 'head', 'scope', 'before', 'after')
    return sha(json.dumps({k: manifest[k] for k in keys}, sort_keys=True).encode())


def capture(repo, scope, base, output):
    repo = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Output must be a new directory')
    output = output.resolve()
    try:
        relative = output.relative_to(repo).as_posix()
    except ValueError:
        relative = None
    if relative and (inside(relative, scope['paths']) or any(inside(p, [relative]) for p in scope['paths'])):
        raise ValueError('Output overlaps task scope')
    commit = git(repo, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
    head = git(repo, 'rev-parse', 'HEAD').decode().strip()
    before_index = index_hash(repo)
    objects = {}
    manifest = {'schema': 1, 'repo': str(repo), 'baseline': commit, 'head': head, 'scope': scope,
                'before': baseline(repo, commit, scope, objects),
                'after': working(repo, scope, objects),
                'index': index_entries(repo, scope, objects), 'index_sha256': before_index}
    if not manifest['before'] and not manifest['after']:
        raise ValueError('Scope selects no files in baseline or candidate; check paths')
    if (manifest['after'] != working(repo, scope, {}) or before_index != index_hash(repo)
            or head != git(repo, 'rev-parse', 'HEAD').decode().strip()):
        raise ValueError('Working tree or index changed during capture; stop writers and retry')
    manifest['id'] = identity(manifest)
    output.mkdir(parents=True)
    (output / 'objects').mkdir()
    for key, data in objects.items():
        (output / 'objects' / key).write_bytes(data)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    lines = []
    for name in sorted(set(manifest['before']) | set(manifest['after'])):
        old, new = manifest['before'].get(name), manifest['after'].get(name)
        if old == new:
            continue
        lines.append('\nFILE ' + name + '\n' + json.dumps({'before': old, 'after': new}) + '\n')
        try:
            a = objects[old['sha256']].decode('utf-8').splitlines(True) if old else []
            b = objects[new['sha256']].decode('utf-8').splitlines(True) if new else []
            lines.extend(difflib.unified_diff(a, b, fromfile='before/' + name, tofile='after/' + name))
        except UnicodeDecodeError:
            lines.append('Binary contents are available in objects/ by SHA256.\n')
    (output / 'changes.diff').write_text(''.join(lines))
    return {'id': manifest['id'], 'manifest': str(output / 'manifest.json')}


def verify(manifest_path, staged=False):
    manifest_path = Path(manifest_path)
    data = json.loads(manifest_path.read_text())
    if data['id'] != identity(data):
        raise ValueError('Manifest identity mismatch')
    for group in ('before', 'after', 'index'):
        for item in data[group].values():
            content = (manifest_path.parent / 'objects' / item['sha256']).read_bytes()
            if sha(content) != item['sha256']:
                raise ValueError('Snapshot object changed')
    repo, scope = Path(data['repo']), data['scope']
    if git(repo, 'rev-parse', 'HEAD').decode().strip() != data['head']:
        raise ValueError('HEAD changed since capture; reconcile commits before new review')
    if working(repo, scope, {}) != data['after']:
        raise ValueError('Candidate has changed since review')
    if staged:
        if index_entries(repo, scope, {}) != data['after']:
            raise ValueError('Staged contents do not match accepted candidate')
        changed = git(repo, 'diff', '--cached', '--no-renames', '--name-only', '-z', 'HEAD').split(b'\0')
        if any(name and not selected(os.fsdecode(name), scope) for name in changed):
            raise ValueError('Staged changes outside reviewed scope; preserve them and isolate commit')
    return {'verified': data['id'], 'staged': staged}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('capture')
    create.add_argument('--repo', type=Path, required=True)
    create.add_argument('--scope', type=Path, required=True)
    create.add_argument('--base', default='HEAD')
    create.add_argument('--out', type=Path, required=True)
    check = commands.add_parser('verify')
    check.add_argument('manifest', type=Path)
    check.add_argument('--staged', action='store_true')
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.repo, scope_config(json.loads(args.scope.read_text())), args.base, args.out)
    else:
        result = verify(args.manifest, args.staged)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
