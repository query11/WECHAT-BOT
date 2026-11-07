"""Isolated reader tests: no production data or TaskJournal construction."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from task_history import read_history


class HistoryTests(unittest.TestCase):
    def test_missing_does_not_create(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(read_history(temp), [])
            self.assertFalse((Path(temp) / '.runtime').exists())

    def test_old_and_new_columns_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '.runtime' / 'tasks.sqlite'
            path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE tasks(id TEXT,label TEXT,kind TEXT,state TEXT,updated REAL,uid TEXT,payload TEXT)')
                db.execute("INSERT INTO tasks VALUES ('a','中文任务','task','running',1,'private-user','private-payload')")
            db.close()
            before = path.read_bytes()
            row = read_history(temp)[0]
            self.assertEqual(row['summary'], '')
            self.assertEqual(row['outputs'], [])
            self.assertEqual(row['state'], 'running')
            self.assertTrue({'id','label','kind','state','updated','summary','outputs'}.issubset(row))
            self.assertNotIn('uid', row)
            self.assertNotIn('payload', row)
            self.assertNotIn('private-user', json.dumps(row))
            self.assertNotIn('private-payload', json.dumps(row))
            self.assertEqual(before, path.read_bytes())
            with sqlite3.connect(path) as db:
                db.execute('ALTER TABLE tasks ADD COLUMN summary TEXT')
                db.execute('ALTER TABLE tasks ADD COLUMN outputs TEXT')
                db.execute('UPDATE tasks SET summary=?,outputs=?', ('结果 sk-secret_hidden',json.dumps(['C:\\private\\报告.docx', 3])))
                db.executemany('INSERT INTO tasks(id,label,kind,state,updated) VALUES (?,?,?,?,?)',
                               [(str(i),'x','task','queued',i+2) for i in range(205)])
            db.close()
            before = path.read_bytes()
            self.assertEqual(len(read_history(temp, 1000)), 200)
            self.assertEqual(read_history(temp)[0]['updated'], 206)
            with sqlite3.connect(path) as db:
                db.execute('UPDATE tasks SET updated=999 WHERE id=\'a\'')
            db.close()
            before = path.read_bytes()
            row = read_history(temp)[0]
            self.assertEqual(row['summary'], '结果 [已隐藏密钥]')
            self.assertEqual(row['outputs'], [r'C:\private\报告.docx'])
            self.assertEqual(before, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
