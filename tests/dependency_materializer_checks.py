#!/usr/bin/env python3
# Independent regression cases, adapted to repository-relative paths.
import hashlib, io, json, os, shutil, stat, subprocess, sys, tarfile, tempfile
from pathlib import Path
SOURCE = Path(__file__).resolve().parents[1]
ROOT = Path(tempfile.mkdtemp(prefix='autodev-dependency-recheck-', dir='/tmp'))
shutil.copy2(SOURCE/'scripts/prepare_dependencies.py', ROOT/'prepare_dependencies-reviewed.py')
shutil.copytree(SOURCE/'dependencies', ROOT/'kit')
print('ROOT', ROOT, flush=True)
print('SCRIPT_SHA256', hashlib.sha256((ROOT/'prepare_dependencies-reviewed.py').read_bytes()).hexdigest(), flush=True)
SCRIPT = ROOT/'prepare_dependencies-reviewed.py'
LOCK = ROOT/'kit/lock.json'
RUN = ROOT/'run'
RUN.mkdir(exist_ok=True)
results=[]
def snapshot(root):
    result={}
    if not root.exists() and not root.is_symlink(): return result
    paths=[root]+sorted(root.rglob('*')) if not root.is_symlink() else [root]
    for path in paths:
        st=path.lstat()
        key='.' if path==root else path.relative_to(root).as_posix()
        data=('link:'+os.readlink(path)) if path.is_symlink() else (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[key]=(st.st_mode,st.st_ino,st.st_mtime_ns,st.st_ctime_ns,data)
    return result

def invoke(dest,lock=LOCK):
    return subprocess.run([sys.executable,str(SCRIPT),'--dest',str(dest),'--lock',str(lock)],capture_output=True,text=True)

def record(name, ok, detail):
    results.append({'case':name,'ok':bool(ok),'detail':detail})
    print(json.dumps(results[-1],ensure_ascii=False),flush=True)

def compare_archive(dest):
    lock=json.loads(LOCK.read_text()); counts={}
    for p in lock['packages']:
        base=dest/p['name']
        with tarfile.open(LOCK.parent/p['archive']) as tar:
            members=tar.getmembers()
            actual={x.relative_to(base).as_posix() for x in base.rglob('*')}
            assert actual=={m.name.rstrip('/') for m in members}
            for m in members:
                path=base/m.name
                if m.issym(): assert path.is_symlink() and os.readlink(path)==m.linkname
                elif m.isdir(): assert path.is_dir() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode)==m.mode
                else: assert path.is_file() and not path.is_symlink() and path.read_bytes()==tar.extractfile(m).read() and stat.S_IMODE(path.stat().st_mode)==m.mode
            counts[p['name']]={'entries':len(members),'links':sum(m.issym() for m in members)}
    return counts

fresh=RUN/'fresh'; sentinel=RUN/'sibling.txt'; sentinel.write_text('foreign content\n'); sibling_before=snapshot(sentinel)
r=invoke(fresh)
record('fresh_install',r.returncode==0,{'returncode':r.returncode,'stderr':r.stderr,'independent_archive_comparison':compare_archive(fresh),'foreign_sibling_unchanged':snapshot(sentinel)==sibling_before})
before=snapshot(fresh); r=invoke(fresh)
record('exact_reuse_without_writes',r.returncode==0 and snapshot(fresh)==before,{'returncode':r.returncode,'same_modes_inodes_mtime_ctime_and_content':snapshot(fresh)==before})
for name, mutate in [
 ('modified_file', lambda d:(d/'superpowers/CLAUDE.md').write_text('changed\n')),
 ('missing_file', lambda d:(d/'superpowers/CLAUDE.md').unlink()),
 ('extra_file', lambda d:(d/'superpowers/foreign.txt').write_text('keep me\n')),
 ('changed_file_mode', lambda d:(d/'superpowers/CLAUDE.md').chmod(0o600)),
 ('changed_dir_mode', lambda d:(d/'superpowers/skills').chmod(0o700)),
 ('changed_symlink', lambda d:((d/'superpowers/AGENTS.md').unlink(),(d/'superpowers/AGENTS.md').symlink_to('README.md'))),
 ('top_level_foreign_file', lambda d:(d/'foreign.txt').write_text('keep me\n')),
]:
 d=RUN/name; shutil.copytree(fresh,d,symlinks=True); mutate(d); before=snapshot(d); r=invoke(d)
 record(name,r.returncode!=0 and snapshot(d)==before,{'returncode':r.returncode,'existing_tree_unchanged':snapshot(d)==before,'error':r.stderr.splitlines()[-1] if r.stderr else None})
for name, setup in [
 ('preexisting_empty_destination',lambda d:d.mkdir()),
 ('preexisting_destination_file',lambda d:d.write_text('foreign data')),
 ('preexisting_destination_symlink',lambda d:d.symlink_to(fresh,target_is_directory=True)),
]:
 d=RUN/name; setup(d); before=snapshot(d); r=invoke(d)
 record(name,r.returncode!=0 and snapshot(d)==before,{'returncode':r.returncode,'existing_tree_unchanged':snapshot(d)==before,'error':r.stderr.splitlines()[-1] if r.stderr else None})
# An otherwise exact package replaced with an external-root symlink.
d=RUN/'package_root_symlink'; shutil.copytree(fresh,d,symlinks=True)
external=RUN/'external-superpowers'; (d/'superpowers').rename(external); (d/'superpowers').symlink_to(external,target_is_directory=True)
before=snapshot(d); ext_before=snapshot(external); r=invoke(d)
record('reject_package_root_symlink',r.returncode!=0,{'returncode':r.returncode,'accepted_external_package':r.returncode==0,'link':str(d/'superpowers'),'target':os.readlink(d/'superpowers'),'trees_unchanged':snapshot(d)==before and snapshot(external)==ext_before})
# Corrupted archives must fail before touching any destination.
kit=RUN/'corrupt-kit'; shutil.copytree(LOCK.parent,kit); a=kit/'elements-05fc4f0.tar'; data=bytearray(a.read_bytes()); data[1024]^=1; a.write_bytes(data)
for existing in [False, True]:
 d=RUN/('corrupt_existing' if existing else 'corrupt_fresh')
 if existing: shutil.copytree(fresh,d,symlinks=True)
 before=snapshot(d); r=invoke(d,kit/'lock.json')
 record('corrupt_archive_'+str(existing),r.returncode!=0 and snapshot(d)==before,{'returncode':r.returncode,'destination_unchanged':snapshot(d)==before,'error':r.stderr.splitlines()[-1] if r.stderr else None})
# Alter the lock's package path while retaining both original archives and hashes.
kit=RUN/'traversal-kit'; shutil.copytree(LOCK.parent,kit); payload=json.loads((kit/'lock.json').read_text()); payload['packages'][0]['name']='../escaped-package'; (kit/'lock.json').write_text(json.dumps(payload))
d=RUN/'package_name_traversal'; escaped=RUN/'escaped-package'; r=invoke(d,kit/'lock.json')
record('reject_package_name_traversal',r.returncode!=0 and not escaped.exists(),{'returncode':r.returncode,'wrote_outside_destination':escaped.exists(),'escaped_path':str(escaped),'destination_entries':sorted(x.name for x in d.iterdir()) if d.exists() else []})

for name, key, replacement in [
 ('absolute_package_name','name',str(RUN/'absolute-package-escape')),
 ('nested_package_name','name','nested/package'),
 ('duplicate_package_name','name','elements-of-style'),
 ('outside_archive_path','archive','../kit/superpowers-b36e082.tar'),
]:
 kit=RUN/name; shutil.copytree(LOCK.parent,kit); payload=json.loads((kit/'lock.json').read_text()); payload['packages'][0][key]=replacement; (kit/'lock.json').write_text(json.dumps(payload))
 d=RUN/('dest-'+name); r=invoke(d,kit/'lock.json')
 record('reject_'+name,r.returncode!=0 and not d.exists(),{'returncode':r.returncode,'destination_absent':not d.exists(),'error':r.stderr.splitlines()[-1] if r.stderr else None})

def malicious_kit(name, members):
    kit=RUN/('tar-'+name); kit.mkdir(); archive=kit/'fixture.tar'
    with tarfile.open(archive,'w') as tar:
        for path,kind,content in members:
            m=tarfile.TarInfo(path); m.mode=0o644
            if kind=='file': m.size=len(content); tar.addfile(m,io.BytesIO(content))
            elif kind=='link': m.type=tarfile.SYMTYPE; m.linkname=content; tar.addfile(m)
            elif kind=='hardlink': m.type=tarfile.LNKTYPE; m.linkname=content; tar.addfile(m)
    payload={'schema':1,'packages':[{'name':'fixture','version':'test','commit':'test','archive':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}]}
    lock=kit/'lock.json'; lock.write_text(json.dumps(payload)); return lock
for name,members in [
 ('parent_traversal',[('../escape','file',b'bad')]),
 ('absolute_path',[(str(RUN/'absolute-escape'),'file',b'bad')]),
 ('duplicate_path',[('same','file',b'a'),('same','file',b'b')]),
 ('symlink_escape',[('link','link','../sibling.txt')]),
 ('hardlink',[('a','file',b'a'),('b','hardlink','a')]),
 ('non_directory_parent',[('a','file',b'a'),('a/child','file',b'b')]),
]:
 lock=malicious_kit(name,members); d=RUN/('rejected-'+name); r=invoke(d,lock)
 record('archive_'+name,r.returncode!=0 and not d.exists(),{'returncode':r.returncode,'destination_absent':not d.exists(),'error':r.stderr.splitlines()[-1] if r.stderr else None})
record('staging_directories_cleaned',not list(RUN.glob('.autodev-sources-*')),[str(p) for p in RUN.glob('.autodev-sources-*')])
print('SYMLINK_MODE', oct(stat.S_IMODE((fresh/'superpowers/AGENTS.md').lstat().st_mode)), flush=True)
(ROOT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
print('TOTAL',len(results),'PASS',sum(x['ok'] for x in results),'FAIL',sum(not x['ok'] for x in results))

sys.exit(0 if all(x['ok'] for x in results) else 1)
