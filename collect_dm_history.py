"""通过已有 DMShoot 历史读取器采集，不调用发送 / Read history using DMShoot; never send."""
import json
import logging
import os
import sys
import time
from pathlib import Path

from reply_memory import ROOT, DMS, SELF_UID, connect
sys.path.insert(0, str(DMS))
logging.disable(logging.CRITICAL)
os.environ['DMSHOOT_BROWSER_CHANNEL'] = 'msedge'
from dmshoot.storage.database import load_config
from dmshoot.plugins.douyin.douyin_client import DouyinClient
from dmshoot.utils.douyin_im_sync import _fetch_raw_via_subprocess
from dmshoot.utils.proto_msg_parser import extract_messages_from_protobuf


def main():
    config = load_config()
    client = DouyinClient(config.douyin_cookie, config.douyin_web_protect, config.douyin_keys)
    ok, _ = client.connect()
    assert ok and client.uid == SELF_UID
    raw, _, _ = _fetch_raw_via_subprocess(config.douyin_cookie)
    db = connect()
    metadata = {'raw_received': bool(raw), 'complete_history': False, 'messages_sent': 0}
    if raw:
        messages = extract_messages_from_protobuf(raw, SELF_UID)
        # 机器解析保留为待核验原料，不能伪装成已验证的人类回复 / Keep parsed history pending validation.
        (ROOT / 'data/dm-history-candidate.json').write_text(json.dumps(messages, ensure_ascii=False), encoding='utf-8')
        metadata.update({'parsed_messages': len(messages), 'outgoing_candidates': sum(bool(m.get('is_self')) for m in messages)})
    db.execute('INSERT INTO scans(source,detail,created_at) VALUES (?,?,?)', ('dm_history', json.dumps(metadata), time.time()))
    db.commit()
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'messages_sent': 0}))
        raise SystemExit(1)
