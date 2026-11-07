import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from task_journal import TaskJournal
from task_history import read_history
from wechat_bridge import Bridge
from tests import selftest_result_feedback

class TaskDetailsTests(unittest.TestCase):
    def test_migration_keeps_old_receipts_and_details_survive_state_only_updates(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'.runtime/tasks.sqlite';path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,uid TEXT,message_id TEXT,kind TEXT,label TEXT,state TEXT,updated REAL,payload TEXT)')
                db.execute("INSERT INTO tasks VALUES('old','owner','m','task','old task','completed',1,NULL)")
            db.close()
            j=TaskJournal(path);j.update('old','completed',summary='saved result',outputs=[str(Path(root)/'result.txt')]);j.update('old','completed')
            row=read_history(root)[0]
            self.assertEqual(row['summary'],'saved result');self.assertEqual(row['state'],'completed');self.assertEqual(len(row['outputs']),1)
    def test_real_reply_and_send_path_capture(self):
        fixture=selftest_result_feedback.ResultFeedbackTests();fixture.setUp();self.addCleanup(fixture.temp.cleanup)
        b=fixture.b;b._worker=threading.current_thread();b.reply=Bridge.reply.__get__(b)
        b.client.send_file.return_value=('report.txt',19);b.client.send_text.return_value=1
        b._send_files(fixture.msg,[str(fixture.path)])
        self.assertIn('接口已接受',fixture.msg._result_summary)
        self.assertEqual(fixture.msg._output_files,[str(fixture.path)])

if __name__=='__main__':unittest.main()
