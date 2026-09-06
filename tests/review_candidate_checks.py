#!/usr/bin/env python3
"""Independent CLI checks for review_candidate.py; all Git fixtures are temporary.

Usage: python3 candidate-review-checks.py [--script /path/review_candidate.py]
Exit 1 means a demonstrated safety/correctness finding, not a harness failure.
The script under review is copied once to the fixture root and never edited.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback

DEFAULT = Path(__file__).resolve().parents[1] / 'scripts/review_candidate.py'
RESULTS = []
ENV = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
ENV.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0')


def run(args, repo=None, env=None):
    return subprocess.run([str(x) for x in args], cwd=repo, env=dict(ENV, **(env or {})),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def git(repo, *args, env=None):
    result = run(['git', '-C', repo, *args], env=env)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result.stdout


def write(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode())


def init(name, files):
    repo = ROOT / name
    repo.mkdir()
    git(repo, 'init', '--initial-branch=main')
    git(repo, 'config', 'user.name', 'Candidate Test')
    git(repo, 'config', 'user.email', 'candidate-test@example.invalid')
    for name, data in files.items():
        write(repo, name, data)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-m', 'fixture baseline')
    return repo


def state(repo):
    files = {}
    for parent, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in dirs[:] + names:
            path = Path(parent) / name
            if name == '.git':
                continue
            mode = path.lstat().st_mode
            relative = path.relative_to(repo).as_posix()
            if stat.S_ISLNK(mode):
                files[relative] = ['link', os.readlink(path)]
            elif stat.S_ISREG(mode):
                files[relative] = ['file', mode & 0o777, hashlib.sha256(path.read_bytes()).hexdigest()]
            elif not stat.S_ISDIR(mode):
                files[relative] = ['other', mode]
    index = Path(os.fsdecode(git(repo, 'rev-parse', '--git-path', 'index')).strip())
    if not index.is_absolute():
        index = repo / index
    return {'files': files, 'index': index.read_bytes() if index.exists() else None,
            'head': git(repo, 'rev-parse', 'HEAD')}


def capture(repo, name, scope, out=None, base=None, preserve=True):
    scope_path = ROOT / (name + '.scope.json')
    scope_path.write_text(json.dumps(scope))
    out = out or ROOT / (name + '.snapshot')
    args = [sys.executable, SCRIPT, 'capture', '--repo', repo, '--scope', scope_path, '--out', out]
    if base:
        args.extend(['--base', base])
    before = state(repo)
    result = run(args)
    if preserve:
        assert state(repo) == before, 'capture mutated HEAD, index bytes, or working files'
    manifest = json.loads(result.stdout)['manifest'] if result.returncode == 0 else None
    return result, Path(manifest) if manifest else None


def verify(repo, manifest, staged=False, env=None):
    before = state(repo)
    result = run([sys.executable, SCRIPT, 'verify', manifest] + (['--staged'] if staged else []), env=env)
    assert state(repo) == before, 'verify mutated HEAD, index bytes, or working files'
    return result


def require(result, succeeds, error=None):
    assert (result.returncode == 0) == succeeds, result.stderr.decode(errors='replace')
    if error:
        assert error.encode() in result.stderr, result.stderr.decode(errors='replace')


def record(name, passed, detail):
    RESULTS.append({'name': name, 'status': 'PASS' if passed else 'FINDING', 'detail': detail})


def comprehensive():
    repo = init('comprehensive', {
        'task/mix.txt': 'base\n', 'task/delete.txt': 'delete\n', 'task/old.txt': 'rename\n',
        'task/bin.bin': b'\x00\xffbase', 'task/file with spaces.txt': 'before\n',
        'task/run.sh': '#!/bin/sh\nexit 0\n', 'owner.txt': 'owner base\n',
        '.gitignore': 'task/ignored*\n'})
    (repo / 'task/link').symlink_to('mix.txt')
    git(repo, 'add', 'task/link')
    git(repo, 'commit', '-m', 'fixture link')
    write(repo, 'task/mix.txt', 'staged version\n')
    git(repo, 'add', 'task/mix.txt')
    write(repo, 'task/mix.txt', 'final unstaged version\n')
    write(repo, 'owner.txt', 'owner staged\n')
    git(repo, 'add', 'owner.txt')
    write(repo, 'owner.txt', 'owner unstaged\n')
    write(repo, 'task/new.txt', 'new\n')
    write(repo, 'task/ignored.bin', b'ignored\x00\xfe')
    (repo / 'task/delete.txt').unlink()
    (repo / 'task/old.txt').rename(repo / 'task/new name.txt')
    write(repo, 'task/bin.bin', b'\x00\xffnew')
    write(repo, 'task/file with spaces.txt', 'after\n')
    (repo / 'task/link').unlink()
    (repo / 'task/link').symlink_to('new name.txt')
    (repo / 'task/run.sh').chmod(0o755)
    result, manifest = capture(repo, 'full', {'paths': ['task']})
    require(result, True)
    data = json.loads(manifest.read_text())
    def content(group, path):
        return (manifest.parent / 'objects' / data[group][path]['sha256']).read_bytes()
    assert content('before', 'task/mix.txt') == b'base\n'
    assert content('index', 'task/mix.txt') == b'staged version\n'
    assert content('after', 'task/mix.txt') == b'final unstaged version\n'
    assert content('after', 'task/ignored.bin') == b'ignored\x00\xfe'
    assert content('after', 'task/bin.bin') == b'\x00\xffnew'
    assert content('after', 'task/link') == b'new name.txt'
    assert data['after']['task/link']['mode'] == '120000'
    assert data['after']['task/run.sh']['mode'] == '100755'
    assert 'task/delete.txt' in data['before'] and 'task/delete.txt' not in data['after']
    assert 'task/old.txt' in data['before'] and 'task/old.txt' not in data['after']
    assert content('after', 'task/new name.txt') == b'rename\n'
    assert content('after', 'task/file with spaces.txt') == b'after\n'
    assert 'owner.txt' not in data['before'] and 'owner.txt' not in data['after']
    require(verify(repo, manifest), True)
    require(verify(repo, manifest, True), False, 'Staged contents do not match accepted candidate')
    record('mixed-content-and-preservation', True,
           'Staged/unstaged contents are distinct; new, ignored, deleted, renamed, binary, symlink, space, executable modes are exact. HEAD/index bytes/working files unchanged.')
    result, repeat = capture(repo, 'repeat-identical', {'paths': ['task']})
    require(result, True)
    assert json.loads(repeat.read_text())['id'] == data['id']
    record('repeat-identical', True, 'Same HEAD, scope and worktree produce same ID.')
    git(repo, 'add', '-f', '-A', '--', 'task')
    result, restaged = capture(repo, 'repeat-restaged', {'paths': ['task']})
    require(result, True)
    restaged_data = json.loads(restaged.read_text())
    assert restaged_data['id'] == data['id']
    assert restaged_data['index_sha256'] != data['index_sha256']
    record('repeat-with-index-change', True, 'Staging same candidate does not alter candidate ID; initial index evidence remains separate.')
    require(verify(repo, manifest, True), False, 'Staged changes outside reviewed scope')
    record('owner-staged-outside', True, 'An ordinary staged owner modification blocks verify --staged; owner index and unstaged contents are preserved.')
    # Build a temporary candidate index to demonstrate isolation without touching owner's index.
    candidate_index = ROOT / 'candidate.index'
    index_env = {'GIT_INDEX_FILE': str(candidate_index)}
    git(repo, 'read-tree', 'HEAD', env=index_env)
    git(repo, 'add', '-f', '-A', '--', 'task', env=index_env)
    require(verify(repo, manifest, True, env=index_env), True)
    names = git(repo, 'diff', '--cached', '--name-only', '--no-renames', env=index_env).decode().splitlines()
    assert names and all(name.startswith('task/') for name in names)
    record('isolated-candidate-index', True, 'A separate GIT_INDEX_FILE admits only scope changes; ordinary owner index remains byte-identical.')
    write(repo, 'task/mix.txt', 'material edit after review\n')
    require(verify(repo, manifest), False, 'Candidate has changed since review')
    require(verify(repo, manifest, True, env=index_env), False, 'Candidate has changed since review')
    result, changed = capture(repo, 'repeat-edited', {'paths': ['task']})
    require(result, True)
    assert json.loads(changed.read_text())['id'] != data['id']
    assert json.loads(changed.read_text())['baseline'] == data['baseline']
    record('post-review-edit', True, 'Material edit with unchanged HEAD rejects old review and produces different candidate ID.')
    object_path = manifest.parent / 'objects' / data['after']['task/new.txt']['sha256']
    object_path.write_bytes(b'tampered')
    require(verify(repo, manifest), False, 'Snapshot object changed')
    record('object-integrity', True, 'Changing saved object bytes is rejected.')


def renamed_outside():
    repo = init('renamed-outside', {'owner.txt': 'unchanged content\n', 'task/keep.txt': 'keep\n'})
    git(repo, 'mv', 'owner.txt', 'task/imported.txt')
    result, manifest = capture(repo, 'renamed-outside', {'paths': ['task']})
    require(result, True)
    result = verify(repo, manifest, True)
    diff = git(repo, 'diff', '--cached', '--name-status').decode().strip()
    if result.returncode == 0:
        git(repo, 'commit', '-m', 'fixture demonstrates wrongly accepted commit')
        committed = git(repo, 'diff', 'HEAD^', 'HEAD', '--name-status', '--no-renames').decode().strip()
        assert 'D\towner.txt' in committed
        record('cross-scope-rename', False,
               'verify --staged accepts deletion outside scope hidden by rename detection. Default diff: ' + diff + '; committed --no-renames: ' + committed)
    else:
        require(result, False, 'Staged changes outside reviewed scope')
        record('cross-scope-rename', True, result.stderr.decode().splitlines()[-1])


def moved_head():
    repo = init('moved-head', {'task/item.txt': 'base\n', 'owner.txt': 'base\n'})
    write(repo, 'task/item.txt', 'reviewed\n')
    result, manifest = capture(repo, 'moved-head', {'paths': ['task']})
    require(result, True)
    base = git(repo, 'rev-parse', 'HEAD').decode().strip()
    write(repo, 'task/item.txt', 'intervening committed version\n')
    git(repo, 'add', 'task/item.txt')
    git(repo, 'commit', '-m', 'intervening unreviewed change')
    write(repo, 'task/item.txt', 'reviewed\n')
    git(repo, 'add', 'task/item.txt')
    normal = verify(repo, manifest)
    staged = verify(repo, manifest, True)
    if staged.returncode != 0:
        require(normal, False, 'HEAD changed since capture')
        require(staged, False, 'HEAD changed since capture')
    record('head-changed-after-review', staged.returncode != 0,
           'Original base ' + base + ', current HEAD ' + git(repo, 'rev-parse', 'HEAD').decode().strip() +
           '; verify exit=' + str(normal.returncode) + ', verify --staged exit=' + str(staged.returncode) +
           '. Reviewed patch base->reviewed differs from actual next commit intervening->reviewed.')
    repo = init('moved-head-outside', {'task/item.txt': 'base\n', 'owner.txt': 'owner base\n'})
    write(repo, 'task/item.txt', 'reviewed\n')
    result, manifest = capture(repo, 'moved-head-outside', {'paths': ['task']})
    require(result, True)
    base = git(repo, 'rev-parse', 'HEAD').decode().strip()
    write(repo, 'owner.txt', 'owner later committed work\n')
    git(repo, 'add', 'owner.txt')
    git(repo, 'commit', '-m', 'owner later work outside scope')
    git(repo, 'restore', '--source=' + base, '--staged', 'owner.txt')
    git(repo, 'add', 'task/item.txt')
    result = verify(repo, manifest, True)
    if result.returncode == 0:
        git(repo, 'commit', '-m', 'fixture demonstrates wrongly accepted owner reversion')
        committed = git(repo, 'diff', 'HEAD^', 'HEAD', '--name-status', '--no-renames').decode().strip()
        assert 'M\towner.txt' in committed
        assert git(repo, 'show', 'HEAD:owner.txt') == b'owner base\n'
        record('head-change-allows-owner-reversion', False,
               'After HEAD changes outside scope, staging old owner blob plus candidate passes --staged against old baseline. Real next commit reverts later owner work: ' + committed)
    else:
        require(result, False, 'HEAD changed since capture')
        record('head-change-allows-owner-reversion', True, result.stderr.decode().splitlines()[-1])


def scope_checks():
    repo = init('scope-checks', {'task/a.txt': 'base\n', 'task/excluded.txt': 'owner base\n', 'outside.txt': 'base\n'})
    write(repo, 'task/a.txt', 'new\n')
    write(repo, 'task/excluded.txt', 'owner new\n')
    for i, scope in enumerate([
        {'paths': ['.']}, {'paths': ['../outside']}, {'paths': ['/tmp']}, {'paths': ['.git/index']},
        {'paths': []}, {'paths': ['task'], 'exclude': [{'path': 'task/excluded.txt', 'reason': ' '}]},
    ]):
        result, _ = capture(repo, 'invalid-' + str(i), scope)
        require(result, False)
    record('invalid-scope', True, 'Root, traversal, absolute, .git, empty paths and exclusion without reason are rejected without mutation.')
    result, manifest = capture(repo, 'excluded', {'paths': ['task'], 'exclude': [{'path': 'task/excluded.txt', 'reason': 'owner change'}]})
    require(result, True)
    data = json.loads(manifest.read_text())
    assert 'task/excluded.txt' not in data['after']
    git(repo, 'add', 'task/a.txt')
    require(verify(repo, manifest, True), True)
    git(repo, 'add', 'task/excluded.txt')
    require(verify(repo, manifest, True), False, 'Staged changes outside reviewed scope')
    record('reasoned-exclusion', True, 'Excluded file is omitted and rejected if staged for ordinary modification.')
    (repo / 'alias').symlink_to('task', target_is_directory=True)
    result, _ = capture(repo, 'symlink-ancestor', {'paths': ['alias/a.txt']})
    require(result, False, 'Symlink ancestor')
    record('symlink-ancestor', True, 'Explicit path below a symlink is rejected.')
    result, _ = capture(repo, 'overlap', {'paths': ['task']}, out=repo.resolve() / 'task' / 'capture')
    require(result, False, 'Output overlaps')
    record('direct-output-overlap', True, 'Direct output/scope overlap is rejected.')
    git(repo, 'init', '--initial-branch=main', 'task/nested')
    result, _ = capture(repo, 'nested-repo', {'paths': ['task']})
    require(result, False, 'Nested repository')
    record('nested-repository', True, 'Nested repository below scope is rejected.')


def output_alias():
    repo = init('output-alias', {'task/a.txt': 'base\n'})
    write(repo, 'task/a.txt', 'reviewed\n')
    alias = ROOT / 'output-parent-alias'
    alias.symlink_to(repo / 'task', target_is_directory=True)
    before = state(repo)
    result, manifest = capture(repo, 'alias', {'paths': ['task']}, out=alias / 'snapshot', preserve=False)
    after = state(repo)
    if result.returncode == 0:
        assert before['index'] == after['index'] and before['head'] == after['head']
        assert before['files'] != after['files']
        failure = verify(repo, manifest)
        require(failure, False, 'Candidate has changed since review')
        record('output-parent-symlink-overlap', False,
               'capture succeeds but writes its snapshot into selected worktree via symlink parent; immediate verify fails. Existing file bytes/index/HEAD preserved, selected area gains snapshot artifacts.')
    else:
        assert before == after
        require(result, False, 'Output overlaps')
        record('output-parent-symlink-overlap', True, 'Aliased scope overlap is rejected.')
    repo = init('output-dotdot', {'task/a.txt': 'base\n'})
    write(repo, 'task/a.txt', 'reviewed\n')
    (repo / 'unselected').mkdir()
    before = state(repo)
    result, manifest = capture(repo, 'dotdot', {'paths': ['task']},
                               out=repo.resolve() / 'unselected' / '..' / 'task' / 'snapshot', preserve=False)
    after = state(repo)
    if result.returncode == 0:
        assert before['files'] != after['files']
        require(verify(repo, manifest), False, 'Candidate has changed since review')
        record('output-dotdot-overlap', False,
               'An output path containing unselected/../task/snapshot also bypasses overlap check and writes artifacts inside scope.')
    else:
        assert before == after
        require(result, False, 'Output overlaps')
        record('output-dotdot-overlap', True, 'Normalized dotdot overlap is rejected.')


def gitlinked_worktree():
    main = init('linked-main', {'task/a.txt': 'base\n', 'owner.txt': 'base\n'})
    repo = ROOT / 'linked-worktree'
    git(main, 'worktree', 'add', '-b', 'candidate-worktree', repo)
    assert (repo / '.git').is_file()
    write(repo, 'task/a.txt', 'staged\n')
    git(repo, 'add', 'task/a.txt')
    write(repo, 'task/a.txt', 'final\n')
    write(repo, 'owner.txt', 'owner unstaged\n')
    result, manifest = capture(repo, 'linked-candidate', {'paths': ['task']})
    require(result, True)
    data = json.loads(manifest.read_text())
    index_path = Path(git(repo, 'rev-parse', '--git-path', 'index').decode().strip())
    assert data['index_sha256'] == hashlib.sha256(index_path.read_bytes()).hexdigest()
    require(verify(repo, manifest), True)
    git(repo, 'add', 'task/a.txt')
    require(verify(repo, manifest, True), True)
    record('gitlinked-worktree', True, 'Git worktree .git file and external per-worktree index are supported; capture/verify preserve index bytes and work files.')


def real_accepted_commit():
    repo = init('accepted-commit', {
        'task/mix.txt': 'base\n', 'task/delete.txt': 'delete\n', 'task/old.txt': 'rename\n',
        'task/bin.bin': b'\x00\xffbase', 'task/file with spaces.txt': 'before\n',
        'owner.txt': 'owner base\n', '.gitignore': 'task/ignored*\n'})
    write(repo, 'task/mix.txt', 'staged\n')
    git(repo, 'add', 'task/mix.txt')
    write(repo, 'task/mix.txt', 'final\n')
    write(repo, 'owner.txt', 'owner staged\n')
    git(repo, 'add', 'owner.txt')
    write(repo, 'owner.txt', 'owner unstaged\n')
    write(repo, 'task/ignored.bin', b'ignored\x00\xfe')
    write(repo, 'task/new.txt', 'new\n')
    write(repo, 'task/bin.bin', b'\x00\xffnew')
    write(repo, 'task/file with spaces.txt', 'after\n')
    (repo / 'task/delete.txt').unlink()
    (repo / 'task/old.txt').rename(repo / 'task/new name.txt')
    (repo / 'task/link').symlink_to('new name.txt')
    result, manifest = capture(repo, 'accepted-commit', {'paths': ['task']})
    require(result, True)
    index_env = {'GIT_INDEX_FILE': str(ROOT / 'real-commit.index')}
    git(repo, 'read-tree', 'HEAD', env=index_env)
    git(repo, 'add', '-f', '-A', '--', 'task', env=index_env)
    require(verify(repo, manifest, True, env=index_env), True)
    original = state(repo)
    git(repo, 'commit', '-m', 'reviewed candidate only', env=index_env)
    after_commit = state(repo)
    assert original['index'] == after_commit['index']
    assert original['files'] == after_commit['files']
    assert original['head'] != after_commit['head']
    committed_names = git(repo, 'diff', 'HEAD^', 'HEAD', '--name-only', '--no-renames').decode().splitlines()
    assert committed_names and all(name.startswith('task/') for name in committed_names)
    assert git(repo, 'show', 'HEAD:owner.txt') == b'owner base\n'
    committed_files = {}
    for row in git(repo, 'ls-tree', '-r', '-z', 'HEAD').split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        name = os.fsdecode(name)
        if not name.startswith('task/'):
            continue
        mode, kind, oid = meta.decode().split()
        content = git(repo, 'cat-file', 'blob', oid)
        committed_files[name] = {'mode': mode, 'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}
    assert committed_files == json.loads(manifest.read_text())['after']
    record('real-accepted-commit', True,
           'After --staged, a real fixture commit through separate index exactly matches every saved scoped blob/mode, including ignored/binary/link/rename/deletion/space. No owner path committed; original owner index bytes and working files preserved.')


def explicit_older_base():
    repo = init('older-base', {'task/a.txt': 'base\n', 'owner.txt': 'owner base\n'})
    write(repo, 'owner.txt', 'owner committed later\n')
    git(repo, 'add', 'owner.txt')
    git(repo, 'commit', '-m', 'prior owner work')
    write(repo, 'task/a.txt', 'reviewed\n')
    result, manifest = capture(repo, 'older-base', {'paths': ['task']}, base='HEAD^')
    require(result, True)
    git(repo, 'add', 'task/a.txt')
    require(verify(repo, manifest, True), True)
    git(repo, 'restore', '--source=HEAD^', '--staged', 'owner.txt')
    require(verify(repo, manifest, True), False, 'Staged changes outside reviewed scope')
    record('explicit-older-base', True,
           'With older --base and fixed HEAD, accepted scoped candidate passes; reverting owner to baseline is still rejected against current HEAD.')


def empty_and_deleted_scope():
    repo = init('empty-and-deleted', {'task/a.txt': 'base\n', 'owner.txt': 'owner base\n'})
    output = ROOT / 'empty-scope.snapshot'
    result, manifest = capture(repo, 'empty-scope', {'paths': ['typo/nonexistent']}, out=output)
    require(result, False, 'Scope selects no files in baseline or candidate; check paths')
    assert manifest is None and not output.exists()
    record('empty-effective-scope-rejected', True,
           'Nonexistent scope is rejected before output directory creation; HEAD/index/work files preserved.')
    (repo / 'task/a.txt').unlink()
    (repo / 'task').rmdir()
    result, manifest = capture(repo, 'full-deletion', {'paths': ['task']})
    require(result, True)
    data = json.loads(manifest.read_text())
    assert set(data['before']) == {'task/a.txt'} and data['after'] == {}
    require(verify(repo, manifest), True)
    git(repo, 'add', '-A', '--', 'task')
    require(verify(repo, manifest, True), True)
    original = state(repo)
    git(repo, 'commit', '-m', 'reviewed complete task directory deletion')
    assert original['files'] == state(repo)['files']
    committed = git(repo, 'diff', 'HEAD^', 'HEAD', '--name-status', '--no-renames').decode().strip()
    assert committed == 'D\ttask/a.txt'
    record('full-scope-deletion', True,
           'Before nonempty / after empty is accepted; --staged succeeds after deletion is staged. Real commit deletes only reviewed task/a.txt.')


def content_and_scope_limits():
    repo = init('normalization', {'task/a.txt': 'base\n', '.gitattributes': '*.txt text eol=crlf\n'})
    write(repo, 'task/a.txt', b'new\r\n')
    result, manifest = capture(repo, 'normalization', {'paths': ['task']})
    require(result, True)
    git(repo, 'add', 'task/a.txt')
    result = verify(repo, manifest, True)
    require(result, False, 'Staged contents do not match accepted candidate')
    record('git-normalization-limit', True,
           'Documented compatibility limit: eol=crlf gives CRLF worktree but LF staged blob, so exact raw-byte comparison blocks valid Git-normalized candidate.')
    repo = init('submodule-index', {'task/a.txt': 'base\n'})
    oid = git(repo, 'rev-parse', 'HEAD').decode().strip()
    git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + oid + ',task/gitlink')
    result, _ = capture(repo, 'submodule-index', {'paths': ['task']})
    require(result, False, 'Unmerged file or submodule')
    record('gitlink-index', True, 'Staged gitlink is rejected, not flattened into normal files.')
    git(repo, 'commit', '-m', 'fixture gitlink')
    result, _ = capture(repo, 'submodule-baseline', {'paths': ['task']})
    require(result, False, 'Submodule in scope')
    record('gitlink-baseline', True, 'Committed gitlink in selected scope is rejected.')
    repo = init('unmerged-index', {'task/a.txt': 'base\n'})
    git(repo, 'checkout', '-b', 'other')
    write(repo, 'task/a.txt', 'other\n')
    git(repo, 'add', 'task/a.txt')
    git(repo, 'commit', '-m', 'other side')
    git(repo, 'checkout', 'main')
    write(repo, 'task/a.txt', 'main\n')
    git(repo, 'add', 'task/a.txt')
    git(repo, 'commit', '-m', 'main side')
    result = run(['git', '-C', repo, 'merge', 'other'])
    assert result.returncode != 0
    result, _ = capture(repo, 'unmerged-index', {'paths': ['task']})
    require(result, False, 'Unmerged file or submodule')
    record('unmerged-index', True, 'Real unresolved merge inside scope is rejected without altering conflicts or index.')
    repo = init('unsupported-file', {'task/a.txt': 'base\n'})
    os.mkfifo(repo / 'task/fifo')
    result, _ = capture(repo, 'unsupported-file', {'paths': ['task']})
    require(result, False, 'Unsupported file type')
    record('unsupported-file-type', True, 'FIFO inside scope is rejected without reading or blocking.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--script', type=Path, default=DEFAULT)
    parser.add_argument('--result', type=Path)
    parser.add_argument('--only', nargs='+', help='Run only named check functions for a targeted follow-up')
    args = parser.parse_args()
    global ROOT, SCRIPT
    ROOT = Path(tempfile.mkdtemp(prefix='candidate-review-', dir='/tmp'))
    if args.result is None:
        args.result = ROOT / 'results.json'
    SCRIPT = ROOT / 'review_candidate.py'
    source = args.script.read_bytes()
    SCRIPT.write_bytes(source)
    checks = [comprehensive, renamed_outside, moved_head, scope_checks, output_alias, gitlinked_worktree,
              real_accepted_commit, explicit_older_base, empty_and_deleted_scope, content_and_scope_limits]
    if args.only:
        assert set(args.only) <= {check.__name__ for check in checks}, 'Unknown check function'
        checks = [check for check in checks if check.__name__ in args.only]
    for check in checks:
        try:
            check()
        except Exception:
            RESULTS.append({'name': check.__name__, 'status': 'HARNESS_ERROR', 'detail': traceback.format_exc()})
    document = {'source': str(args.script), 'source_sha256': hashlib.sha256(source).hexdigest(),
                'fixture_root': str(ROOT), 'python': sys.version, 'git': git(ROOT, '--version').decode().strip(),
                'results': RESULTS}
    args.result.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return int(any(x['status'] != 'PASS' for x in RESULTS))


if __name__ == '__main__':
    sys.exit(main())
