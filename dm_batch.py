"""按需私信批次：新鲜快照、有限轮次、逐条验收 / On-demand DM batches with bounded verified sending."""
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import reply_memory as memory


def peer_for(conversation_id, owner=memory.SELF_UID):
    """只接受本人参与的一对一会话 / Accept only one-to-one conversations involving this account."""
    match = re.fullmatch(r'0:1:(\d+):(\d+)(?::0:?)?', str(conversation_id))
    if not match or owner not in match.groups() or match[1] == match[2]:
        return None
    return match[2] if match[1] == owner else match[1]


def message_time(server_id):
    """未知时间不回退成现在 / Never replace an unknown server timestamp with the current time."""
    try:
        value = int(server_id) >> 32
        return float(value) if 1_577_836_800 <= value <= time.time() + 86400 else None
    except (TypeError, ValueError):
        return None


def wire_fields(raw):
    """读取Protobuf线格式，不扫描正文猜字段 / Decode wire fields rather than guessing inside text."""
    fields, offset = {}, 0
    def varint():
        nonlocal offset
        number = 0
        for shift in range(0, 70, 7):
            if offset >= len(raw):
                raise ValueError('Incomplete varint')
            b = raw[offset]
            offset += 1
            number |= (b & 127) << shift
            if not b & 128:
                return number
        raise ValueError('Oversized varint')
    while offset < len(raw):
        tag = varint()
        field, kind = tag >> 3, tag & 7
        if not field:
            raise ValueError('Invalid field')
        if kind == 0:
            value = varint()
        elif kind in (1, 2, 5):
            size = varint() if kind == 2 else (8 if kind == 1 else 4)
            if size < 0 or offset + size > len(raw):
                raise ValueError('Invalid length')
            value = raw[offset:offset+size]
            offset += size
        else:
            raise ValueError('Unsupported wire type')
        fields.setdefault(field, []).append((kind, value))
    return fields


def parse_snapshot(raw, owner=memory.SELF_UID):
    """只保留结构可证实的消息体，未知结构失败关闭 / Keep structurally validated message bodies only."""
    if not raw or len(raw) > 32 * 1024 * 1024:
        raise RuntimeError('Missing or oversized history snapshot.')
    found, remaining = {}, [100000]
    def walk(blob, depth=0, inherited=''):
        remaining[0] -= 1
        if depth > 16 or remaining[0] <= 0:
            raise RuntimeError('Snapshot parsing limit exceeded.')
        try:
            fields = wire_fields(blob)
        except ValueError:
            return
        def one(n, kind):
            values = fields.get(n, [])
            return values[0][1] if len(values) == 1 and values[0][0] == kind else None
        conversation = inherited
        first = one(1, 2)
        if first:
            candidate = first.decode('utf-8', errors='replace')
            if peer_for(candidate, owner):
                conversation = candidate
        peer = peer_for(conversation, owner)
        mid, sender, body, kind = one(3, 0), one(7, 0), one(8, 2), one(6, 0)
        if peer and mid and sender and body and kind is not None and str(sender) in (owner, peer):
            try:
                content = json.loads(body)
                text = content.get('text') if isinstance(content, dict) else None
                item = {'source_id': 'douyin:'+str(mid), 'peer_uid': peer, 'thread_id': 'dm:'+owner+':'+peer,
                        'sender_uid': str(sender), 'is_self': str(sender)==owner, 'text': text if isinstance(text,str) else '',
                        'kind': 'text' if kind == 7 and isinstance(text,str) else 'unsupported',
                        'timestamp': message_time(mid), 'index': one(4,0) or 0}
                if item['source_id'] in found and found[item['source_id']] != item:
                    raise RuntimeError('Conflicting message identity in snapshot.')
                found[item['source_id']] = item
            except (ValueError, UnicodeDecodeError):
                pass
        for values in fields.values():
            for wire_type, child in values:
                if wire_type == 2 and len(child) > 8:
                    walk(child, depth+1, conversation)
    walk(raw)
    if not found:
        raise RuntimeError('No structurally verified messages; cannot send from cached guesses.')
    return list(found.values())


def save_snapshot(db, rows):
    db.execute('''CREATE TABLE IF NOT EXISTS dm_live_messages (
       source_id TEXT PRIMARY KEY, thread_id TEXT, peer_uid TEXT, sender_uid TEXT,
       is_self INTEGER, text TEXT, kind TEXT, source_time REAL, message_index INTEGER, seen_at REAL)''')
    for r in rows:
        db.execute('INSERT OR REPLACE INTO dm_live_messages VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (r['source_id'],r['thread_id'],r['peer_uid'],r['sender_uid'],int(r['is_self']),r['text'],r['kind'],r['timestamp'],r['index'],time.time()))
    db.commit()


def make_candidates(db, rows):
    """最后一条必须是未回答的新入站 / The last message must be a fresh unanswered inbound."""
    groups = defaultdict(list)
    for row in rows:
        groups[row['thread_id']].append(row)
    result = []
    for thread, items in groups.items():
        if any(item['timestamp'] is None for item in items):
            continue
        items.sort(key=lambda r:(r['timestamp'],r['index'],int(r['source_id'].split(':')[-1])))
        last = items[-1]
        if last['is_self'] or not memory.in_reply_window(last['timestamp']):
            continue
        if db.execute("SELECT 1 FROM handoffs WHERE channel='dm' AND thread_id=? AND status='awaiting_user'",(thread,)).fetchone():
            continue
        if db.execute("SELECT 1 FROM send_attempts WHERE channel='dm' AND source_id=?",(last['source_id'],)).fetchone():
            continue
        inbound = []
        for item in reversed(items):
            if item['is_self']:
                break
            if memory.in_reply_window(item['timestamp']):
                inbound.append(item)
        inbound.reverse()
        prior = db.execute("SELECT max(created_at) FROM send_attempts WHERE channel='dm' AND thread_id=?",(thread,)).fetchone()[0]
        if prior is not None and last['timestamp'] <= prior:
            continue
        candidate = dict(last)
        candidate['input'] = '\n'.join(item['text'] for item in inbound)
        candidate['fingerprint'] = tuple(item['source_id'] for item in inbound)
        candidate['context'] = '\n'.join(('我：' if i['is_self'] else '对方：') + i['text'] for i in items[-8:])
        candidate['must_hold'] = any(i['kind']!='text' for i in inbound) or memory.dm_reply_budget(db,thread,last['timestamp'])<=0
        result.append(candidate)
    return sorted(result,key=lambda r:r['timestamp'])


class DouyinTransport:
    """复用已验证SDK；快照必须现取 / Use the verified SDK with fresh snapshots."""
    def __init__(self):
        sys.path.insert(0,str(memory.DMS))
        os.environ['DMSHOOT_BROWSER_CHANNEL'] = 'msedge'
        logging.disable(logging.CRITICAL)
        from dmshoot.storage.database import load_config
        from dmshoot.plugins.douyin.douyin_client import DouyinClient
        config = load_config()
        if config.auto_reply_enabled:
            raise RuntimeError('Disable the legacy automatic-reply switch before running a bounded batch.')
        self.client = DouyinClient(config.douyin_cookie,config.douyin_web_protect,config.douyin_keys)
        ok, _ = self.client.connect()
        if not ok or self.client.uid != memory.SELF_UID or not self.client.has_send_credentials:
            raise RuntimeError('Account identity or signing credentials are not ready.')
        self.cookie = config.douyin_cookie
        self.ws = None
        self.thread = None
        self.events = queue.Queue()
        self.receiver_ready = threading.Event()

    def snapshot(self):
        from dmshoot.utils.douyin_im_sync import _fetch_raw_via_subprocess
        raw, _, _ = _fetch_raw_via_subprocess(self.cookie)
        return parse_snapshot(raw)

    def seed_prior_sends(self, db):
        """迁入已确认的旧测试发送，不重置同会话次数 / Count the previously verified test delivery."""
        from contextlib import closing
        with closing(memory.source_db(memory.DMS/'dmshoot/data/dmshoot.db')) as src:
            if not src.execute("SELECT 1 FROM sqlite_master WHERE name='approved_test_credential_retry'").fetchone():
                return
            prior = src.execute("SELECT * FROM approved_test_credential_retry WHERE source_id=1 AND status='api_acknowledged'").fetchone()
            inbound = src.execute('SELECT * FROM messages WHERE id=1 AND is_self=0').fetchone()
            if not prior or not inbound or inbound['content']!='测试自动回复':
                return
            peer = peer_for(inbound['session_id'].removeprefix('douyin:'))
            echo = src.execute('SELECT * FROM messages WHERE is_self=1 AND content=? AND timestamp>=? ORDER BY timestamp LIMIT 1',
                               (memory.TEST_REPLY,prior['attempted_at']-2)).fetchone()
            if not peer or not echo:
                return
            db.execute('INSERT OR IGNORE INTO send_attempts VALUES (?,?,?,?,?,?,?)',
                       ('dm','legacy-test:'+inbound['message_key'],'dm:'+memory.SELF_UID+':'+peer,memory.TEST_REPLY,'verified',echo['message_key'],prior['attempted_at']))
            existing = db.execute("SELECT value FROM runtime_meta WHERE key='last_write_attempt_at'").fetchone()
            if not existing or float(existing[0])<prior['attempted_at']:
                db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)',('last_write_attempt_at',str(prior['attempted_at'])))
            db.commit()

    def prepare_send(self):
        if self.receiver_ready.is_set():
            return
        from dmshoot.utils.douyin_ws import _WrappedWS
        sys.path.insert(0,str(memory.DMS/'external/DouYin_Spider/dy_apis'))
        self.ws = _WrappedWS(self.client.auth,self.events,on_open=self.receiver_ready.set)
        def run():
            try:
                self.ws.start()
            except Exception:
                pass
        self.thread = threading.Thread(target=run,daemon=True)
        self.thread.start()
        if not self.receiver_ready.wait(20):
            raise RuntimeError('Unable to establish delivery verification; nothing sent.')

    def send(self, candidate, text):
        from dy_apis.douyin_api import DouyinAPI
        self.prepare_send()
        started = time.time()
        cid,sid,ticket = DouyinAPI.create_conversation(self.client.auth,int(candidate['peer_uid']))
        if peer_for(cid) != candidate['peer_uid'] or not ticket:
            raise RuntimeError('Destination verification failed.')
        if not DouyinAPI.send_msg(self.client.auth,cid,sid,ticket,text):
            raise RuntimeError('Send response not acknowledged; no retry.')
        end = time.monotonic()+20
        while time.monotonic()<end:
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                continue
            timestamp = message_time(event.get('server_message_id'))
            if (str(event.get('sender_uid'))==memory.SELF_UID and peer_for(event.get('conversation_id'))==candidate['peer_uid']
                and event.get('content')==text and timestamp is not None and timestamp>=started-2):
                return {'reply_id':'douyin:'+str(event['server_message_id']),'verified':True}
        raise RuntimeError('Acknowledged but echo missing; stop without retry.')

    def close(self):
        if self.ws:
            self.ws.close()
        if self.thread:
            self.thread.join(timeout=3)


def run_batch(db, send=False, transport=None):
    policy = json.loads((memory.ROOT/'policy.json').read_text(encoding='utf-8'))
    if policy['mode']!='on_demand' or policy['background_monitoring']:
        raise RuntimeError('Only on-demand mode is allowed.')
    if send and db.execute("SELECT 1 FROM send_attempts WHERE status IN ('attempting','uncertain_stop_no_retry')").fetchone():
        raise RuntimeError('Unresolved delivery exists; review before continuing.')
    if (memory.ROOT/'data/comment-run.lock').exists():
        raise RuntimeError('A legacy batch lock exists; review it first.')
    lock = memory.ROOT/'data/reply-run.lock'
    handle = lock.open('x',encoding='utf-8')
    results = []
    try:
        handle.write(str(time.time()))
        handle.close()
        transport = transport or DouyinTransport()
        if hasattr(transport,'seed_prior_sends'):
            transport.seed_prior_sends(db)
        rows = transport.snapshot()
        save_snapshot(db,rows)
        candidates = make_candidates(db,rows)
        for item in candidates[:policy['max_replies_per_run']]:
            if item['must_hold']:
                proposal = {'reply':'','hold':True,'reason':'已达回复上限或包含不支持的消息类型','sample_ids':[]}
            else:
                proposal = memory.draft(db,'dm',item['input'],item['context'])
            db.commit()
            result = {'source_id':item['source_id'],'status':'held' if proposal['hold'] else 'draft','reply':proposal['reply'],'sample_ids':proposal['sample_ids']}
            results.append(result)
            if proposal['hold']:
                db.execute('INSERT OR IGNORE INTO handoffs VALUES (?,?,?,?,?,?,?,?)',('dm',item['source_id'],item['thread_id'],item['input'],proposal['reply'],proposal.get('reason') or '需本人处理','awaiting_user',time.time()))
                db.commit()
                continue
            if not send or not proposal['reply'].strip():
                continue
            prior = db.execute("SELECT value FROM runtime_meta WHERE key='last_write_attempt_at'").fetchone()
            while prior and time.time()-float(prior[0])<policy['minimum_interval_seconds']:
                delay = max(0.0,policy['minimum_interval_seconds']-(time.time()-float(prior[0])))
                if delay:
                    time.sleep(min(30,delay))
            fresh_rows = transport.snapshot()
            save_snapshot(db,fresh_rows)
            fresh = next((c for c in make_candidates(db,fresh_rows) if c['thread_id']==item['thread_id']),None)
            if not fresh or fresh['fingerprint']!=item['fingerprint'] or fresh['input']!=item['input'] or fresh['must_hold']:
                result['status']='skipped_changed'
                continue
            transport.prepare_send()
            db.execute('INSERT INTO send_attempts VALUES (?,?,?,?,?,?,?)',('dm',item['source_id'],item['thread_id'],proposal['reply'],'attempting',None,time.time()))
            db.execute('INSERT OR REPLACE INTO runtime_meta VALUES (?,?)',('last_write_attempt_at',str(time.time())))
            db.commit()
            try:
                delivery = transport.send(fresh,proposal['reply'])
                if not delivery.get('verified') or not delivery.get('reply_id'):
                    raise RuntimeError('Delivery unverified.')
                db.execute('UPDATE send_attempts SET status=?,reply_id=? WHERE channel=? AND source_id=?',('verified',delivery['reply_id'],'dm',item['source_id']))
                db.commit()
                result['status']='verified'
            except Exception:
                db.execute('UPDATE send_attempts SET status=? WHERE channel=? AND source_id=?',('uncertain_stop_no_retry','dm',item['source_id']))
                db.commit()
                raise
        return {'candidate_count':len(candidates),'results':results,'snapshot_messages':len(rows),'complete_history':False,
                'sent_count':sum(r['status']=='verified' for r in results)}
    finally:
        if transport:
            transport.close()
        if not handle.closed:
            handle.close()
        lock.unlink(missing_ok=True)
