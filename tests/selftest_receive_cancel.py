import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import aes128
from bridge_inbox import InboxStore
from ilink_client import Message
from task_journal import TaskJournal
from task_control import TaskControl
from claude_runner import TaskCancelled
from tests import selftest_standby
from tests.selftest_inbox_recv import file_item,KEY

PLAIN=b'incoming cancellation fixture'
class DownloadHandler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path=='/slow':self.server.slow_started.set();self.server.release.wait(10)
        try:
            self.send_response(200);self.send_header('Content-Length',str(len(self.server.body)));self.end_headers();self.wfile.write(self.server.body)
        except OSError:pass

class ReceiveCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),DownloadHandler);self.server.daemon_threads=True
        self.server.body=aes128.encrypt_ecb_pkcs7(KEY,PLAIN)
        self.server.slow_started=threading.Event();self.server.release=threading.Event()
        threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.shutdown)
        fixture=selftest_standby.StandbyTests();fixture.setUp();self.b=fixture.bridge
        b=self.b;b.inbox_dir=str(self.root/'inbox');b.inbox_store=InboxStore(str(self.root/'index.json'))
        b.inbox_max_bytes=1024*1024;b.inbox_quota_bytes=0;b._enqueue=Mock(return_value=None)
        b.journal=TaskJournal(self.root/'tasks.sqlite');b.enable_typing=False;b.notice_after=0
    def shutdown(self):
        self.server.release.set();self.server.shutdown();self.server.server_close()
    def start(self,paths,body='read incoming files'):
        items=[file_item(plain=PLAIN,name='part%d.txt'%n,url='http://127.0.0.1:%d%s'%(self.server.server_port,path)) for n,path in enumerate(paths)]
        msg=Message({'from_user_id':'owner','message_id':'incoming','item_list':items})
        msg._inbox_enqueued_at=time.monotonic()
        msg._inbox_journal_id=self.b.journal.add('owner','incoming','inbox','receive')[0]
        self.b._inbox_q.put((msg,'owner',body));self.b._inbox_q.put(None)
        worker=threading.Thread(target=self.b._inbox_loop);worker.start();return msg,worker
    def test_cancel_slow_download_cleans_partial_and_skips_analysis(self):
        msg,worker=self.start(['/slow']);self.assertTrue(self.server.slow_started.wait(5))
        self.assertIn('已请求停止接收',self.b._stop_running('owner'))
        worker.join(5);self.assertFalse(worker.is_alive())
        self.assertEqual(self.b.journal.recent()[0]['state'],'cancelled')
        self.assertEqual(list((self.root/'inbox').iterdir()),[])
        self.b._enqueue.assert_not_called();self.assertEqual(self.b._inbox_q.unfinished_tasks,0)
    def test_completed_first_file_survives_cancel_of_second(self):
        msg,worker=self.start(['/fast','/slow']);self.assertTrue(self.server.slow_started.wait(5))
        self.b._stop_running('owner');worker.join(5);self.assertFalse(worker.is_alive())
        files=list((self.root/'inbox').iterdir());self.assertEqual(len(files),1);self.assertEqual(files[0].read_bytes(),PLAIN)
        self.assertEqual(len(self.b.inbox_store.all()),1);self.b._enqueue.assert_not_called()
        self.assertEqual(self.b.journal.recent()[0]['state'],'cancelled')
    def test_normal_receive_keeps_integrity_and_analysis(self):
        msg,worker=self.start(['/fast']);worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(self.b.journal.recent()[0]['state'],'completed')
        self.b._enqueue.assert_called_once();self.assertEqual(next((self.root/'inbox').iterdir()).read_bytes(),PLAIN)
    def test_cancel_at_commit_boundary_removes_uncommitted_file(self):
        control=TaskControl()
        info={'name':'boundary.txt','kind':'文件','url':'http://unused','key_hex':KEY.hex(),'size':len(PLAIN)}
        from ilink_client import media_download_info
        info=media_download_info(file_item(plain=PLAIN,url='http://unused'))
        def downloaded(info,tmp,limit,token):
            Path(tmp).write_bytes(PLAIN);token.cancel();return len(PLAIN),hashlib.md5(PLAIN).hexdigest()
        with patch('bridge_inbox.receive_cancellable',side_effect=downloaded):
            with self.assertRaises(TaskCancelled):self.b._inbox_save_one(info,'owner',1,control)
        self.assertEqual(list((self.root/'inbox').iterdir()),[]);self.assertEqual(self.b.inbox_store.all(),[])

    def test_stopall_skips_queued_receives_and_allows_new_messages(self):
        for ident in ('queued1','queued2'):
            msg=Message({'from_user_id':'owner','message_id':ident,'item_list':[file_item(plain=PLAIN,url='http://127.0.0.1:%d/slow'%self.server.server_port)]})
            msg._inbox_enqueued_at=time.monotonic()
            msg._inbox_journal_id=self.b.journal.add('owner',ident,'inbox','receive')[0]
            self.b._inbox_q.put((msg,'owner','analyze'))
        self.b._inbox_q.put(None)
        self.assertIn('排队的附件',self.b._stop_running('owner',True))
        worker=threading.Thread(target=self.b._inbox_loop);worker.start();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertFalse(self.server.slow_started.is_set())
        self.assertTrue(all(r['state']=='cancelled' for r in self.b.journal.recent()))
        self.b._enqueue.assert_not_called()
        msg,worker=self.start(['/fast']);worker.join(5)
        self.assertEqual(self.b.journal.recent()[0]['state'],'completed')

if __name__=='__main__':unittest.main()
