"""新进程和隔离恢复的回归测试 / Fresh-process and isolated-restore regressions."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reply_memory as memory
import release_tools


class V1RecoveryTests(unittest.TestCase):
    def test_send_history_handoff_and_interval_survive_new_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'state.sqlite3'
            db = memory.connect(path)
            db.execute('INSERT INTO send_attempts VALUES (?,?,?,?,?,?,?)', ('comment','old','v','sent','verified','r',1))
            db.execute('INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?)', ('comment','held','v','question','','uncertain','awaiting_user',1))
            db.execute('INSERT INTO runtime_meta VALUES (?,?)', ('last_write_attempt_at','123'))
            db.commit()
            db.close()
            code = "import sqlite3,sys,json; d=sqlite3.connect(sys.argv[1]); print(json.dumps([d.execute('SELECT count(*) FROM send_attempts').fetchone()[0],d.execute('SELECT count(*) FROM handoffs').fetchone()[0],d.execute('SELECT value FROM runtime_meta').fetchone()[0]]))"
            result = subprocess.check_output([sys.executable,'-c',code,str(path)],cwd=temporary,text=True)
            self.assertEqual(json.loads(result),[1,1,'123'])

    def test_backup_does_not_change_live_config_or_include_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root/'data'
            comments = root/'comments'
            dms = root/'dms'
            paths = [data/'reply-memory.sqlite3',comments/'storage/douyin.db',dms/'dmshoot/data/dmshoot.db']
            for path in paths:
                path.parent.mkdir(parents=True,exist_ok=True)
                with sqlite3.connect(path) as db:
                    db.executescript("CREATE TABLE config(key TEXT,value TEXT); CREATE TABLE messages(id INTEGER,content TEXT);")
                    db.execute('INSERT INTO config VALUES (?,?)', ('api_key','test-secret-not-real'))
                    db.execute('INSERT INTO messages VALUES (?,?)',(1,'example'))
                db.close()
            with patch.object(release_tools,'DATA',data),patch.object(memory,'COMMENTS',comments),patch.object(memory,'DMS',dms):
                result = release_tools.backup()
            self.assertTrue(result['ok'])
            backup = Path(result['backup'])
            for source, name in zip(paths,['reply-memory.sqlite3','comment-state.sqlite3','dm-state.sqlite3']):
                with sqlite3.connect(source) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM config').fetchone()[0],1)
                db.close()
                with sqlite3.connect(backup/name) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM config').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
                db.close()
                self.assertNotIn(b'test-secret-not-real',(backup/name).read_bytes())


if __name__ == '__main__':
    unittest.main()
