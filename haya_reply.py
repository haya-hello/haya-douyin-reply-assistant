"""固定入口与无外发维护命令 / Stable entry and maintenance without social posting."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

import reply_memory as memory

ROOT = memory.ROOT
DATA = ROOT / 'data'
RUNTIME = DATA / 'runtime'
BRIDGE = 'http://127.0.0.1:19422'
PYTHON = memory.DMS / '.venv/Scripts/python.exe'


def json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def fetch(url, key=None):
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=10) as response:
        return json.load(response)


def run(args, cwd=ROOT, timeout=60):
    process = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode:
        raise RuntimeError('Subprocess failed; review its designated test report.')
    return process.stdout.strip()


def state_snapshot():
    db = memory.connect()
    result = {'policy': json_file(ROOT / 'policy.json'), 'version': json_file(ROOT / 'version.json')['version']}
    result['db_integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
    result['tables'] = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                        for name in ('samples','dm_history','send_attempts','handoffs','runtime_meta')}
    result['unresolved_sends'] = db.execute("SELECT count(*) FROM send_attempts WHERE status IN ('attempting','uncertain_stop_no_retry')").fetchone()[0]
    result['last_write_attempt_at'] = (db.execute("SELECT value FROM runtime_meta WHERE key='last_write_attempt_at'").fetchone() or [None])[0]
    result['run_lock_present'] = (DATA / 'comment-run.lock').exists()
    db.close()
    try:
        remote = fetch(BRIDGE + '/api/status')
        result['bridge'] = {'online': remote.get('ok') is True, 'connections': remote.get('totalConnections', 0)}
    except Exception:
        result['bridge'] = {'online': False, 'connections': 0}
    result['dm_batch'] = 'not_implemented'
    return result


def doctor(online=False):
    checks = {}
    state = state_snapshot()
    checks['database'] = state['db_integrity'] == 'ok'
    checks['policy_72_hours'] = state['policy'].get('reply_window_days') == 3
    checks['on_demand_only'] = state['policy'].get('mode') == 'on_demand' and not state['policy'].get('background_monitoring')
    checks['no_unresolved_send'] = state['unresolved_sends'] == 0 and not state['run_lock_present']
    checks['python_available'] = PYTHON.is_file()
    checks['node_available'] = shutil.which('node') is not None
    try:
        settings = json_file(memory.COMMENTS / 'config.json')
        script = (memory.COMMENTS / 'local/douyin.user.js').read_text(encoding='utf-8')
        checks['local_configuration'] = bool(settings['llm']['api_key'] and settings['bridge']['token'])
        checks['script_token_matches'] = ("token: '" + settings['bridge']['token'] + "'") in script
        checks['loopback_only'] = settings['bridge']['host'] == '127.0.0.1'
    except Exception:
        settings = None
        checks['local_configuration'] = False
    if (DATA / 'releases/v1.0.0/manifest.json').exists():
        import release_tools
        checks['frozen_source_matches'] = release_tools.verify_release()['ok']
    else:
        checks['frozen_source_matches'] = False
    if online:
        checks['bridge_online'] = state['bridge']['online']
        checks['browser_connected'] = state['bridge']['connections'] > 0
        try:
            videos = memory.cli('my', '--count', '1')
            checks['account_identity'] = bool(videos and videos[0].get('owner_uid') == memory.SELF_UID)
        except Exception:
            checks['account_identity'] = False
        try:
            models = fetch(settings['llm']['base_url'].rstrip('/') + '/models', settings['llm']['api_key'])
            checks['model_gateway'] = settings['llm']['model'] in {m['id'] for m in models.get('data', [])}
        except Exception:
            checks['model_gateway'] = False
    result = {'ok': all(checks.values()), 'checks': checks, 'state': state, 'social_messages_sent': 0}
    write_json(DATA / 'qa/doctor-latest.json', result)
    return result


def bridge_start():
    try:
        health = fetch(BRIDGE + '/api/health')
        if health.get('ok'):
            return {'started': False, 'already_online': True, 'instance_id': health.get('instance_id')}
    except Exception:
        pass
    if not shutil.which('node'):
        raise RuntimeError('Node.js missing; no installation was attempted.')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    instance = uuid.uuid4().hex
    env = dict(os.environ, HAYA_REPLY_BRIDGE_INSTANCE=instance)
    with (RUNTIME / 'bridge.log').open('ab') as log:
        child = subprocess.Popen([shutil.which('node'), str(memory.COMMENTS / 'server.js')], cwd=memory.COMMENTS,
                                  env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    write_json(RUNTIME / 'bridge.json', {'pid': child.pid, 'instance_id': instance, 'server': str(memory.COMMENTS / 'server.js')})
    for _ in range(20):
        try:
            health = fetch(BRIDGE + '/api/health')
            if health.get('instance_id') == instance:
                return {'started': True, 'pid': child.pid, 'instance_id': instance}
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError('Bridge startup could not be confirmed; inspect data/runtime/bridge.log.')


def bridge_stop():
    record_path = RUNTIME / 'bridge.json'
    if not record_path.exists():
        raise RuntimeError('No managed process record; will not stop an unknown service.')
    record = json_file(record_path)
    health = fetch(BRIDGE + '/api/health')
    if health.get('instance_id') != record['instance_id']:
        raise RuntimeError('Service identity changed; stop refused.')
    pid = int(record['pid'])
    # 仅停止身份匹配的本项目服务 / Stop only this project's verified managed process.
    if os.name == 'nt':
        run(['powershell.exe','-NoProfile','-Command',f'Stop-Process -Id {pid} -ErrorAction Stop'])
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
    return {'stopped': True, 'pid': pid, 'instance_id': record['instance_id']}


def restart_check():
    """重启服务和新进程读取，不重启系统、不外发 / Restart bridge and reload state; no OS reboot or posting."""
    before = state_snapshot()
    if before['unresolved_sends'] or before['run_lock_present']:
        raise RuntimeError('Pending work must be reviewed before restart acceptance.')
    stopped = bridge_stop()
    started = bridge_start()
    reloaded = json.loads(run([PYTHON, '-X', 'utf8', __file__, 'status'], cwd=DATA))
    for _ in range(25):
        if fetch(BRIDGE + '/api/status').get('totalConnections', 0):
            break
        time.sleep(1)
    live = doctor(online=True)
    preserved = before['tables'] == reloaded['tables'] and before['policy'] == reloaded['policy'] and before['last_write_attempt_at'] == reloaded['last_write_attempt_at']
    report = {'ok': preserved and live['ok'] and stopped['instance_id'] != started['instance_id'],
              'state_preserved': preserved, 'new_bridge_instance': stopped['instance_id'] != started['instance_id'],
              'fresh_process_loaded_from_other_cwd': True, 'doctor': live['checks'],
              'os_reboot_performed': False, 'new_chat_created': False, 'social_messages_sent': 0}
    write_json(DATA / 'qa/restart-acceptance.json', report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', nargs='?', default='status', choices=['status','doctor','start','stop','restart-check','backup','freeze','verify-release','restore-check','preview','reply','handoffs'])
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--confirm-send', action='store_true')
    args = parser.parse_args()
    if args.action == 'status': result = state_snapshot()
    elif args.action == 'doctor': result = doctor(args.online)
    elif args.action == 'start': result = bridge_start()
    elif args.action == 'stop': result = bridge_stop()
    elif args.action == 'restart-check': result = restart_check()
    elif args.action in ('backup','freeze','verify-release','restore-check'):
        import release_tools
        result = getattr(release_tools, args.action.replace('-', '_'))()
    elif args.action in ('preview','reply'):
        if args.action == 'reply' and not args.confirm_send:
            raise RuntimeError('Actual sending requires --confirm-send and a current user reply request.')
        if not doctor(True)['ok']:
            raise RuntimeError('Preflight failed; inspect doctor-latest.json. No messages sent.')
        db = memory.connect()
        result = memory.reply_comments(db, 20, send=args.action == 'reply')
        db.commit()
        db.close()
    else:
        result = json.loads(run([PYTHON,'-X','utf8', ROOT/'reply_memory.py','handoffs']))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get('ok') is False:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # 不输出请求或凭据 / Never dump requests or credentials in errors.
        print(json.dumps({'ok': False, 'error_type': type(error).__name__, 'detail': str(error) if isinstance(error, RuntimeError) else 'Check local configuration and reports.'}, ensure_ascii=False))
        raise SystemExit(1)
