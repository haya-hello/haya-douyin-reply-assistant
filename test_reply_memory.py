"""回复库隔离与去重测试 / Corpus isolation and deduplication tests."""
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import reply_memory as memory


class ReplyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = memory.connect(self.root / 'memory.db')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_import_is_idempotent(self):
        for _ in range(2):
            memory.save_sample(self.db, 'comment', '1', 'v', '谢谢分享', '谢谢支持')
        self.assertEqual(self.db.execute('SELECT count(*) FROM samples').fetchone()[0], 1)

    def test_channel_isolation(self):
        memory.save_sample(self.db, 'dm', '1', 'private', '怎么联系', '仅私聊内容')
        self.assertEqual(memory.search(self.db, 'comment', '怎么联系'), [])

    def test_generated_reply_not_used_as_style(self):
        memory.save_sample(self.db, 'comment', '1', 'v', '谢谢分享', '系统生成', provenance='tool_generated')
        self.assertEqual(memory.search(self.db, 'comment', '谢谢分享'), [])

    def test_similar_reply_found(self):
        memory.save_sample(self.db, 'comment', '1', 'v', '谢谢分享', '谢谢支持')
        self.assertEqual(memory.search(self.db, 'comment', '谢谢分享')[0]['id'], '1')

    def test_contacts_redacted(self):
        text = memory.scrub('https://example.com/x a@example.com 13800138000 微信:abc12345 sk-abcdef12345')
        for secret in ('example.com', '13800138000', 'abc12345', 'sk-abcdef12345'):
            self.assertNotIn(secret, text)

    def test_incomplete_pair_not_eligible(self):
        memory.save_sample(self.db, 'dm', '1', 't', '', '没有上文', provenance='unpaired_outgoing')
        self.assertEqual(self.db.execute('SELECT eligible FROM samples').fetchone()[0], 0)

    def run_mock_batch(self, send=False, fail_send=False, hold=False):
        (self.root / 'data').mkdir(exist_ok=True)
        (self.root / 'policy.json').write_text(json.dumps({'mode':'on_demand','background_monitoring':False,'minimum_interval_seconds':60,'max_replies_per_run':1}))
        video = {'aweme_id':'v', 'owner_uid':memory.SELF_UID, 'desc':'视频'}
        parent = {'cid':'c','text':'谢谢分享','time':memory.time.time()-10,'user':{'uid':'other'},'children':[]}
        calls = []
        sent = False
        def command(*args):
            nonlocal sent
            calls.append(args[0])
            if args[0] == 'my':
                return [video]
            if args[0] == 'get':
                if sent:
                    return [{**parent,'children':[{'cid':'reply','user':{'uid':memory.SELF_UID}}]}]
                return [parent]
            if args[0] == 'post':
                if fail_send:
                    raise RuntimeError('timeout')
                sent = True
                return {'cid':'reply','status':'published'}
        with patch.object(memory, 'ROOT', self.root), patch.object(memory, 'cli', side_effect=command), patch.object(memory, 'draft', return_value={'reply':'谢谢','hold':hold,'sample_ids':[]}):
            result = memory.reply_comments(self.db, 1, send)
        return result, calls

    def test_preview_never_posts(self):
        result, calls = self.run_mock_batch(False)
        self.assertNotIn('post', calls)
        self.assertEqual(result[0]['status'], 'draft')

    def test_explicit_run_posts_and_verifies_once(self):
        result, calls = self.run_mock_batch(True)
        self.assertEqual(calls.count('post'), 1)
        self.assertEqual(result[0]['status'], 'verified')
        self.assertEqual(self.db.execute('SELECT status FROM send_attempts').fetchone()[0], 'verified')

    def test_uncertain_delivery_blocks_future_runs(self):
        with self.assertRaises(RuntimeError):
            self.run_mock_batch(True, True)
        self.assertEqual(self.db.execute('SELECT status FROM send_attempts').fetchone()[0], 'uncertain_stop_no_retry')
        with self.assertRaises(RuntimeError):
            self.run_mock_batch(True)

    def test_manually_replied_comment_is_skipped(self):
        comment = {'text':'谢谢','user':{'uid':'other'},'children':[{'user':{'uid':memory.SELF_UID}}]}
        self.assertFalse(memory.eligible_comment(comment))

    def test_comment_is_not_sent_again_in_new_run(self):
        self.run_mock_batch(True)
        result, calls = self.run_mock_batch(True)
        self.assertEqual(result, [])
        self.assertNotIn('post', calls)

    def test_held_comment_stays_for_user_across_runs(self):
        result, calls = self.run_mock_batch(True, hold=True)
        self.assertEqual(result[0]['status'], 'held')
        self.assertNotIn('post', calls)
        result, calls = self.run_mock_batch(True, hold=False)
        self.assertEqual(result, [])
        self.assertNotIn('post', calls)
        self.assertEqual(self.db.execute('SELECT status FROM handoffs').fetchone()[0], 'awaiting_user')

    def test_dm_budget_counts_across_runs(self):
        (self.root / 'policy.json').write_text('{"dm_max_auto_replies_per_conversation":2}')
        with patch.object(memory, 'ROOT', self.root):
            self.assertEqual(memory.dm_reply_budget(self.db, 'conversation', memory.time.time()-10), 2)
            for i in range(2):
                self.db.execute('INSERT INTO send_attempts VALUES (?,?,?,?,?,?,?)', ('dm', str(i), 'conversation', 'test', 'verified', str(i), i))
            self.assertEqual(memory.dm_reply_budget(self.db, 'conversation', memory.time.time()-10), 0)

    def test_rolling_three_day_boundary(self):
        now = 1_800_000_000
        self.assertTrue(memory.in_reply_window(now-3*86400, now))
        self.assertFalse(memory.in_reply_window(now-3*86400-1, now))
        self.assertTrue(memory.in_reply_window((now-60)*1000, now))
        for bad in (None, '', True, 0, 'not-a-date', float('nan'), float('inf'), now+1):
            self.assertFalse(memory.in_reply_window(bad, now))

    def test_old_comment_is_skipped_even_when_unanswered(self):
        c = {'text':'谢谢','time':memory.time.time()-4*86400,'user':{'uid':'other'}}
        self.assertFalse(memory.eligible_comment(c))

    def test_dm_old_message_has_no_budget(self):
        (self.root / 'policy.json').write_text('{"dm_max_auto_replies_per_conversation":2,"reply_window_days":3}')
        with patch.object(memory, 'ROOT', self.root):
            self.assertEqual(memory.dm_reply_budget(self.db, 'new', memory.time.time()-4*86400), 0)
            self.assertEqual(memory.dm_reply_budget(self.db, 'new'), 0)


if __name__ == '__main__':
    unittest.main()
