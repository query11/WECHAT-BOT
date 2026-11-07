"""Real isolated transfer subprocess and local HTTP server; no WeChat/API traffic."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from ilink_client import ILinkClient
from task_control import TaskControl,TransferCancelled
from claude_runner import TaskCancelled

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def reply(self,data):
        raw=json.dumps(data).encode()
        try:
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except OSError:pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        if self.path.endswith('/getuploadurl'):
            self.server.url_requests+=1
            self.reply({'errcode':0,'upload_full_url':'http://127.0.0.1:%d/upload'%self.server.server_port})
        elif self.path=='/upload':
            self.server.uploading.set()
            if self.server.mode=='upload':self.server.release.wait(10)
            self.reply({'errcode':0,'download_param':'local-test-download'})
        elif self.path.endswith('/sendmessage'):
            self.server.published+=1;self.server.publishing.set()
            if self.server.mode=='publish':self.server.release.wait(10)
            self.reply({'errcode':0})
        else:self.reply({'errcode':0})

class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.file=self.root/'sample.txt';self.file.write_bytes(b'local transfer fixture')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.daemon_threads=True
        self.server.mode='pass';self.server.url_requests=0;self.server.published=0
        self.server.uploading=threading.Event();self.server.publishing=threading.Event();self.server.release=threading.Event()
        threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.shutdown)
        self.client=ILinkClient('http://127.0.0.1:%d'%self.server.server_port,'local-token','local-bot',state_dir=str(self.root))
        self.control=TaskControl()
    def shutdown(self):
        self.server.release.set();self.server.shutdown();self.server.server_close()
    def start(self):
        result={}
        def run():
            try:result['value']=self.client.send_file('owner',str(self.file),context_token='local-context',control=self.control)
            except BaseException as e:result['error']=e
        t=threading.Thread(target=run);t.start();return t,result
    def test_success_uses_real_protocol_and_acceptance(self):
        t,r=self.start();t.join(10)
        self.assertFalse(t.is_alive());self.assertEqual(r.get('value'),('sample.txt',22));self.assertEqual(self.server.published,1)
    def test_pre_cancel_starts_no_request(self):
        self.control.cancel();t,r=self.start();t.join(3)
        self.assertIsInstance(r.get('error'),TaskCancelled);self.assertEqual(self.server.url_requests,0)
    def test_cancel_while_encrypting_never_uploads(self):
        self.file.write_bytes(b'x'*(8*1024*1024));t,r=self.start();deadline=time.monotonic()+5
        while getattr(self.control,'transfer_phase','')!='encrypting' and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(getattr(self.control,'transfer_phase',''),'encrypting')
        self.control.cancel();t.join(5)
        self.assertFalse(t.is_alive());self.assertIsInstance(r.get('error'),TaskCancelled)
        self.assertEqual(self.server.url_requests,0);self.assertEqual(self.server.published,0)
    def test_cancel_during_cdn_prevents_message_publication(self):
        self.server.mode='upload';t,r=self.start();self.assertTrue(self.server.uploading.wait(5))
        self.control.cancel();t.join(5)
        self.assertFalse(t.is_alive());self.assertIsInstance(r.get('error'),TransferCancelled)
        self.assertFalse(r['error'].uncertain);self.assertEqual(self.server.published,0)
    def test_cancel_during_publication_reports_uncertain(self):
        self.server.mode='publish';t,r=self.start();self.assertTrue(self.server.publishing.wait(5))
        self.control.cancel();t.join(5)
        self.assertFalse(t.is_alive());self.assertIsInstance(r.get('error'),TransferCancelled)
        self.assertTrue(r['error'].uncertain);self.assertEqual(self.server.published,1)

    def test_bridge_stop_upload_without_model_handle(self):
        self.run_bridge_stop('upload','cancelled')
    def test_bridge_stop_publication_records_uncertain(self):
        self.run_bridge_stop('publish','uncertain')
    def run_bridge_stop(self,mode,expected):
        import queue
        from unittest.mock import Mock
        from ilink_client import Message
        from task_journal import TaskJournal
        from tests.selftest_standby import StandbyTests
        class OneQueue(queue.Queue):
            used=False
            def get(self,*args,**kwargs):
                if self.used:raise SystemExit()
                self.used=True;return super().get(*args,**kwargs)
        fixture=StandbyTests();fixture.setUp();b=fixture.bridge
        b.queue=OneQueue();b.client=self.client;b.runner=Mock();b.runner.cancel.return_value=False
        b._set_stage=Mock();b.send_roots=[str(self.root)];b.send_file_max_mb=20;b.send_file_limit=3
        b.journal=TaskJournal(self.root/'tasks.sqlite')
        msg=Message({'from_user_id':'owner','message_id':'transfer-test','context_token':'local-context'})
        msg._journal_id=b.journal.add('owner','transfer-test','sendfile','test')[0]
        b.queue.put((msg,'',{'sendfile':[str(self.file)]}));self.server.mode=mode
        worker=threading.Thread(target=b._loop_worker);b._worker=worker;worker.start()
        event=self.server.uploading if mode=='upload' else self.server.publishing
        self.assertTrue(event.wait(5))
        self.assertTrue(b._stop_running('owner').startswith('⏹'))
        worker.join(5);self.assertFalse(worker.is_alive())
        self.assertEqual(b.journal.recent()[0]['state'],expected)
        self.assertEqual(b.queue.unfinished_tasks,0);self.assertIsNone(b._running)
        text='\n'.join(call.args[1] for call in b.reply.call_args_list)
        self.assertIn('未确认' if expected=='uncertain' else '已叫停',text)

if __name__=='__main__':unittest.main()
