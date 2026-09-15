"""私有版本快照与隔离恢复验收 / Private releases and isolated restore verification."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import reply_memory as memory

ROOT = memory.ROOT
DATA = ROOT / 'data'
RELEASE = DATA / 'releases/v1.0.0'
ROOTS = {'reply-assistant': ROOT, 'douyin-cli': memory.COMMENTS, 'DMShoot': memory.DMS}
BLOCKED_PARTS = {'data','local','storage','logs','reports','screenshots','__pycache__','.venv','node_modules','.npm-cache','.git','.wheels','.auth'}


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            sha.update(block)
    return sha.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def command(*args, cwd=ROOT):
    return subprocess.check_output([str(a) for a in args], cwd=cwd, text=True, encoding='utf-8').strip()


def source_files():
    result = {}
    for label, root in ROOTS.items():
        if label == 'reply-assistant':
            paths = [p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()]
        else:
            output = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'], cwd=root).decode('utf-8')
            paths = list(set(output.split('\0')) - {''})
        if label == 'DMShoot':
            paths.append('external/DouYin_Spider/package-lock.json')
        for relative in sorted(set(paths)):
            rel = Path(relative)
            if any(part in BLOCKED_PARTS for part in rel.parts):
                continue
            if rel.name in ('config.json','local-llm.json','compact_log.txt','test_qr.png') or rel.suffix in ('.log','.db','.sqlite3','.stackdump','.pyc'):
                continue
            target = root / rel
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root.resolve()):
                continue
            result[label + '/' + rel.as_posix()] = target
    return result


def freeze():
    if RELEASE.exists():
        raise RuntimeError('V1 release already exists; never overwrite a frozen release.')
    files = source_files()
    # 阻止运行配置混入源码包 / Prevent live configuration from entering the source archive.
    assert not any('/data/' in name or '/local/' in name or name.endswith('/config.json') for name in files)
    RELEASE.mkdir(parents=True)
    manifest = {'version':'1.0.0', 'created_at':datetime.now(timezone.utc).isoformat(),
                'files':{}, 'upstream':{}, 'runtime':{}, 'contains_runtime_credentials':False,
                'credentials_restore':'Reconfigure model credentials and re-login; never restore them from this bundle.'}
    with zipfile.ZipFile(RELEASE / 'source.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in files.items():
            archive.write(path, name)
            manifest['files'][name] = digest(path)
    for label in ('douyin-cli','DMShoot'):
        root = ROOTS[label]
        patch = subprocess.check_output(['git','diff','--binary','HEAD'], cwd=root)
        (RELEASE / f'{label}.patch').write_bytes(patch)
        manifest['upstream'][label] = {'commit':command('git','rev-parse','HEAD',cwd=root),
                                      'remote':command('git','remote','get-url','origin',cwd=root)}
    interpreter = memory.DMS / '.venv/Scripts/python.exe'
    packages = json.loads(command(interpreter,'-m','pip','list','--format=json','--disable-pip-version-check'))
    (RELEASE / 'requirements.freeze.txt').write_text('\n'.join(f"{p['name']}=={p['version']}" for p in sorted(packages,key=lambda p:p['name'].lower()))+'\n', encoding='utf-8')
    manifest['runtime'] = {'node':command('node','--version'),'python':command(interpreter,'--version')}
    manifest['artifacts'] = {p.name:digest(p) for p in RELEASE.iterdir() if p.is_file()}
    write_json(RELEASE/'manifest.json', manifest)
    return {'ok':True,'release':str(RELEASE),'source_files':len(files),'runtime':manifest['runtime']}


def verify_release():
    manifest = json.loads((RELEASE/'manifest.json').read_text(encoding='utf-8'))
    current = source_files()
    changed = [name for name, sha in manifest['files'].items() if name not in current or digest(current[name]) != sha]
    added = sorted(set(current)-set(manifest['files']))
    broken = [name for name, sha in manifest['artifacts'].items() if not (RELEASE/name).is_file() or digest(RELEASE/name) != sha]
    return {'ok':not changed and not added and not broken,'changed_sources':changed,'added_sources':added,'broken_artifacts':broken}


def logical_digest(path, omit_config=True):
    sha = hashlib.sha256()
    with closing(memory.source_db(path)) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            if omit_config and name == 'config':
                continue
            quoted = '"' + name.replace('"','""') + '"'
            schema = db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0]
            sha.update(schema.encode())
            rows = [json.dumps(list(r),ensure_ascii=False,default=lambda value:value.hex() if isinstance(value,bytes) else str(value)) for r in db.execute('SELECT * FROM '+quoted)]
            for row in sorted(rows):
                sha.update(row.encode())
    return sha.hexdigest()


def backup():
    if (DATA/'comment-run.lock').exists():
        raise RuntimeError('An active or interrupted batch exists; review before snapshotting.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    output = DATA/'backups'/stamp
    output.mkdir(parents=True)
    sources = {'reply-memory.sqlite3':DATA/'reply-memory.sqlite3',
               'comment-state.sqlite3':memory.COMMENTS/'storage/douyin.db',
               'dm-state.sqlite3':memory.DMS/'dmshoot/data/dmshoot.db'}
    manifest = {'created_at':datetime.now(timezone.utc).isoformat(),'files':{},'private_data':True,
                'config_credentials_excluded':True,'restore_policy':'Restore into a new directory first; re-authenticate separately.'}
    for name, source in sources.items():
        target = output/name
        with memory.source_db(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
            exists = dst.execute("SELECT 1 FROM sqlite_master WHERE name='config'").fetchone()
            if exists:
                # 只清理新备份副本中的配置，不触碰原库 / Strip config only from the new backup, never the live DB.
                dst.execute('PRAGMA secure_delete=ON')
                dst.execute('DELETE FROM config')
                dst.commit()
                dst.execute('VACUUM')
            assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        dst.close()
        src.close()
        manifest['files'][name] = {'sha256':digest(target),'logical_sha256':logical_digest(target),
                                  'source_nonconfig_matches':logical_digest(source)==logical_digest(target)}
    write_json(output/'manifest.json', manifest)
    return {'ok':all(v['source_nonconfig_matches'] for v in manifest['files'].values()),'backup':str(output),'databases':len(sources),'config_credentials_excluded':True}


def restore_check():
    """恢复到新隔离目录，绝不覆盖生产 / Restore only into a fresh isolated directory."""
    manifest = json.loads((RELEASE/'manifest.json').read_text(encoding='utf-8'))
    backups = sorted((DATA/'backups').glob('*/manifest.json'))
    if not backups:
        raise RuntimeError('No data backup is available.')
    saved = json.loads(backups[-1].read_text(encoding='utf-8'))
    output = DATA/'restore-checks'/uuid.uuid4().hex
    output.mkdir(parents=True)
    checks = []
    if digest(RELEASE/'source.zip') != manifest['artifacts']['source.zip']:
        raise RuntimeError('Source archive checksum mismatch.')
    with zipfile.ZipFile(RELEASE/'source.zip') as archive:
        if set(archive.namelist()) != set(manifest['files']):
            raise RuntimeError('Archive contents differ from the release manifest.')
        for name in archive.namelist():
            target = (output/'source'/name).resolve()
            if not target.is_relative_to((output/'source').resolve()):
                raise RuntimeError('Unsafe archive path.')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.read(name))
            checks.append(digest(target)==manifest['files'][name])
    for name, info in saved['files'].items():
        source = backups[-1].parent/name
        if digest(source) != info['sha256']:
            raise RuntimeError('Data backup checksum mismatch.')
        target = output/name
        with memory.source_db(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
            checks.append(dst.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
            if dst.execute("SELECT 1 FROM sqlite_master WHERE name='config'").fetchone():
                checks.append(dst.execute('SELECT count(*) FROM config').fetchone()[0]==0)
        dst.close()
        src.close()
        checks.append(logical_digest(target)==info['logical_sha256'])
    result = {'ok':all(checks),'restored_to':str(output),'source_files':len(manifest['files']),
              'databases':len(saved['files']),'production_overwritten':False,'credentials_restored':False}
    qa = DATA/'qa'
    qa.mkdir(exist_ok=True)
    write_json(qa/'restore-acceptance.json', result)
    return result
