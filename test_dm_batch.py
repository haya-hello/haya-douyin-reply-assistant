"""私信批次安全回归，全部模拟发送 / DM batch regressions; all sends are mocked."""
import json
import tempfile
import time
import sys
import queue
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import dm_batch
import reply_memory as memory

NOW = 1_800_000_000
PEER = '123456789123'


def row(number=1, own=False, peer=PEER, timestamp=NOW-10, kind='text'):
    return {'source_id':'douyin:'+str(number),'thread_id':'dm:'+memory.SELF_UID+':'+peer,
            'peer_uid':peer,'sender_uid':memory.SELF_UID if own else peer,'is_self':own,
            'timestamp':timestamp,'index':number,'text':'测试内容','kind':kind}


class FakeTransport:
    def __init__(self, snapshots, fail=False):
        self.snapshots=snapshots
        self.reads=0
        self.sent=[]
        self.fail=fail
        self.closed=False
    def snapshot(self):
        result=self.snapshots[min(self.reads,len(self.snapshots)-1)]
        self.reads+=1
        return result
    def prepare_send(self):
        pass
    def send(self,item,text):
        self.sent.append(item['source_id'])
        if self.fail:
            raise RuntimeError('Simulated uncertain delivery')
        return {'reply_id':'echo:'+item['source_id'],'verified':True}
    def close(self):
        self.closed=True


class DMBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'data').mkdir()
        (self.root/'policy.json').write_text(json.dumps({'mode':'on_demand','background_monitoring':False,
            'reply_window_days':3,'dm_max_auto_replies_per_conversation':2,'max_replies_per_run':5,'minimum_interval_seconds':0}))
        self.db=memory.connect(self.root/'data/test.db')
        self.patches=[patch.object(memory,'ROOT',self.root),patch.object(memory.time,'time',return_value=NOW),
                      patch.object(memory,'draft',return_value={'reply':'已了解','hold':False,'sample_ids':[]})]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.db.close()
        self.temp.cleanup()
    def attempt(self,source,created=NOW-100,status='verified'):
        self.db.execute('INSERT INTO send_attempts VALUES (?,?,?,?,?,?,?)',('dm',source,row()['thread_id'],'reply',status,'echo',created))
        self.db.commit()
    def test_preview_does_not_send(self):
        t=FakeTransport([[row()]])
        result=dm_batch.run_batch(self.db,transport=t)
        self.assertEqual(result['candidate_count'],1)
        self.assertEqual(t.sent,[])
        self.assertTrue(t.closed)
    def test_verified_send_and_no_duplicate_on_next_run(self):
        t=FakeTransport([[row()]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['sent_count'],1)
        again=FakeTransport([[row()]])
        self.assertEqual(dm_batch.run_batch(self.db,True,again)['sent_count'],0)
    def test_two_reply_cap_hands_off(self):
        self.attempt('old1')
        self.attempt('old2')
        t=FakeTransport([[row()]])
        result=dm_batch.run_batch(self.db,True,t)
        self.assertEqual(result['results'][0]['status'],'held')
        self.assertEqual(t.sent,[])
        self.assertEqual(self.db.execute('SELECT status FROM handoffs').fetchone()[0],'awaiting_user')
    def test_second_reply_needs_new_inbound(self):
        self.attempt('old',created=NOW-5)
        t=FakeTransport([[row(timestamp=NOW-10)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['candidate_count'],0)
        t=FakeTransport([[row(2,timestamp=NOW-1)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['sent_count'],1)
    def test_manual_reply_during_generation_skips(self):
        t=FakeTransport([[row()],[row(),row(2,own=True,timestamp=NOW-1)]])
        result=dm_batch.run_batch(self.db,True,t)
        self.assertEqual(result['results'][0]['status'],'skipped_changed')
        self.assertEqual(t.sent,[])
    def test_new_question_during_generation_skips_stale_draft(self):
        t=FakeTransport([[row()],[row(),row(2,timestamp=NOW-1)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['results'][0]['status'],'skipped_changed')
    def test_uncertain_send_stops_entire_batch_and_future_runs(self):
        t=FakeTransport([[row(),row(2,peer='987654321987')]],fail=True)
        with self.assertRaises(RuntimeError): dm_batch.run_batch(self.db,True,t)
        self.assertEqual(len(t.sent),1)
        with self.assertRaises(RuntimeError): dm_batch.run_batch(self.db,True,FakeTransport([[row(3)]]))
    def test_old_and_unknown_dates_never_send(self):
        t=FakeTransport([[row(timestamp=NOW-4*86400),row(2,peer='987654321987',timestamp=None)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['candidate_count'],0)
    def test_latest_outgoing_means_already_answered(self):
        t=FakeTransport([[row(),row(2,own=True,timestamp=NOW-1)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['candidate_count'],0)
    def test_hold_is_persistent(self):
        with patch.object(memory,'draft',return_value={'reply':'','hold':True,'reason':'不确定','sample_ids':[]}):
            dm_batch.run_batch(self.db,True,FakeTransport([[row()]]))
        t=FakeTransport([[row(2,timestamp=NOW-1)]])
        self.assertEqual(dm_batch.run_batch(self.db,True,t)['candidate_count'],0)
    def test_shared_lock_prevents_parallel_send(self):
        (self.root/'data/reply-run.lock').write_text('other-run')
        with self.assertRaises(FileExistsError): dm_batch.run_batch(self.db,True,FakeTransport([[row()]]))
    def test_non_text_message_is_handed_off(self):
        result=dm_batch.run_batch(self.db,True,FakeTransport([[row(kind='unsupported')]]))
        self.assertEqual(result['results'][0]['status'],'held')

    def test_multiple_conversations_are_processed_sequentially(self):
        t=FakeTransport([[row(),row(2,peer='987654321987')]])
        result=dm_batch.run_batch(self.db,True,t)
        self.assertEqual(result['sent_count'],2)
        self.assertEqual(t.sent,['douyin:1','douyin:2'])


class TransportDeliveryTests(unittest.TestCase):
    def transport(self):
        t=object.__new__(dm_batch.DouyinTransport)
        t.client=SimpleNamespace(auth=object())
        t.events=queue.Queue()
        t.prepare_send=lambda:None
        return t
    def test_destination_is_checked_before_posting(self):
        calls=[]
        api=SimpleNamespace(create_conversation=lambda *_:(f'0:1:999:{memory.SELF_UID}',1,'ticket'),
                            send_msg=lambda *a:calls.append(a))
        with patch.dict(sys.modules,{'dy_apis.douyin_api':SimpleNamespace(DouyinAPI=api)}):
            with self.assertRaises(RuntimeError): self.transport().send(row(),'reply')
        self.assertEqual(calls,[])
    def test_server_ack_and_matching_echo_are_both_required(self):
        t=self.transport()
        mid=str(int(time.time())<<32)
        t.events.put({'server_message_id':mid,'sender_uid':memory.SELF_UID,
                      'conversation_id':f'0:1:{PEER}:{memory.SELF_UID}','content':'reply'})
        api=SimpleNamespace(create_conversation=lambda *_:(f'0:1:{PEER}:{memory.SELF_UID}',1,'ticket'),send_msg=lambda *_:True)
        with patch.dict(sys.modules,{'dy_apis.douyin_api':SimpleNamespace(DouyinAPI=api)}):
            self.assertEqual(t.send(row(),'reply'),{'reply_id':'douyin:'+mid,'verified':True})


def vint(value):
    result=bytearray()
    while value>127:
        result.append((value&127)|128)
        value>>=7
    return bytes(result)+bytes([value])


def field(number,value):
    if isinstance(value,int): return vint(number<<3)+vint(value)
    return vint((number<<3)|2)+vint(len(value))+value


class WireSnapshotTests(unittest.TestCase):
    def fixture(self,owner=memory.SELF_UID):
        return b''.join([field(1,f'0:1:{PEER}:{owner}'.encode()),field(2,1),
          field(3,(int(time.time())-10)<<32),field(4,1),field(5,123),field(6,7),field(7,int(PEER)),
          field(8,json.dumps({'text':'hello'}).encode())])
    def test_nested_message_body_is_parsed(self):
        result=dm_batch.parse_snapshot(field(6,field(8,self.fixture())))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['peer_uid'],PEER)
        self.assertEqual(result[0]['text'],'hello')
    def test_other_account_is_rejected(self):
        with self.assertRaises(RuntimeError): dm_batch.parse_snapshot(self.fixture('999999999999'))
    def test_text_that_looks_like_credentials_is_not_a_message(self):
        with self.assertRaises(RuntimeError): dm_batch.parse_snapshot(b'{"text":"0:1:1:2"}')
    def test_unknown_id_has_no_current_time_fallback(self):
        self.assertIsNone(dm_batch.message_time('123'))


if __name__=='__main__': unittest.main()
