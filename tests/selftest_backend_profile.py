import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.error
import backend_profile as profile
from key_vault import read_secret,is_encrypted
from claude_api_runner import ClaudeAPIRunner

KEY='sk-profile-offline-placeholder-987654321'
class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.raw={'backend':'deepseek','unrelated':{'keep':'value'},'deepseek':{'model':'deepseek-flash','key_file':'original.key'}}
        self.path=self.root/'config.json';self.path.write_text(json.dumps(self.raw),encoding='utf-8-sig')
        (self.root/'original.key').write_text(KEY,encoding='utf-8')
        p=patch.dict(os.environ,{'DEEPSEEK_API_KEY':''});p.start();self.addCleanup(p.stop)
    def request(self,**kwargs):
        return dict(operation='save',provider='deepseek',model='deepseek-flash',api_key='',revision=profile.view(self.root)['revision'],**kwargs)
    def test_view_never_exposes_key(self):
        v=profile.view(self.root);self.assertTrue(v['deepseek']['configured']);self.assertNotIn(KEY,json.dumps(v))
    def test_keep_key_and_preserve_other_settings(self):
        r=self.request();r['model']='deepseek-v4-pro'
        out=profile.execute(self.root,r);self.assertTrue(out['ok'])
        raw=json.loads(self.path.read_text(encoding='utf-8-sig'))
        self.assertEqual(raw['deepseek']['key_file'],'original.key');self.assertEqual(raw['unrelated'],self.raw['unrelated'])
        self.assertEqual((self.root/'original.key').read_text(),KEY)
    def test_replace_key_versioned_and_not_in_config_or_response(self):
        r=self.request();r['api_key']='sk-new-profile-placeholder-12345678'
        out=profile.execute(self.root,r);self.assertTrue(out['ok'])
        raw=json.loads(self.path.read_text(encoding='utf-8-sig'))
        self.assertEqual(read_secret(self.root/raw['deepseek']['key_file']),r['api_key'])
        self.assertTrue(is_encrypted(self.root/raw['deepseek']['key_file']))
        self.assertEqual((self.root/'original.key').read_text(),KEY)
        self.assertNotIn(r['api_key'],json.dumps(out));self.assertNotIn(r['api_key'],self.path.read_text(encoding='utf-8-sig'))
    def test_conflict_and_write_error_preserve_original(self):
        r=self.request();before=self.path.read_bytes();r['revision']='stale'
        self.assertFalse(profile.execute(self.root,r)['ok']);self.assertEqual(before,self.path.read_bytes())
        r=self.request();r['api_key']='sk-new-profile-placeholder-12345678'
        with patch('backend_profile.os.replace',side_effect=OSError('disk full')):
            self.assertFalse(profile.execute(self.root,r)['ok'])
        self.assertEqual(before,self.path.read_bytes());self.assertFalse(list((self.root/'.runtime').glob('*.key')))
    def test_custom_claude_and_restore_original(self):
        r=self.request();r.update(provider='claude',enabled=True,base_url='https://example.invalid/anthropic',model='test-model',api_key='custom-token-1234')
        self.assertTrue(profile.execute(self.root,r)['ok'])
        raw=json.loads(self.path.read_text(encoding='utf-8-sig'));self.assertTrue(raw['claude_api']['enabled'])
        runner=ClaudeAPIRunner(workdir=str(self.root),base_url=raw['claude_api']['base_url'],model='test-model',key_file=self.root/raw['claude_api']['key_file'],session_file=str(self.root/'sessions.json'))
        env=runner._process_options()['env'];self.assertEqual(env['ANTHROPIC_BASE_URL'],'https://example.invalid/anthropic')
        self.assertNotIn('custom-token-1234',str(runner._build_cmd(None,'request')))
        r.update(enabled=False,revision=profile.view(self.root)['revision'],api_key='')
        self.assertTrue(profile.execute(self.root,r)['ok']);self.assertFalse(profile.view(self.root)['claude']['enabled'])
    def test_invalid_addresses_and_models_do_not_save(self):
        for url in ('http://example.invalid','https://name:password@example.invalid','https://example.invalid?key=bad'):
            r=self.request();r.update(provider='claude',enabled=True,base_url=url,model='model',api_key='some-token')
            self.assertFalse(profile.execute(self.root,r)['ok'])
        r=self.request();r['model']='bad-model';self.assertFalse(profile.execute(self.root,r)['ok'])
    def test_connection_does_not_save_and_does_not_return_remote_text(self):
        r=self.request();r['operation']='test';before=self.path.read_bytes()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'content':[{'type':'text','text':KEY}]}).encode()
        opener=Mock();opener.open.return_value=response
        with patch('backend_profile.urllib.request.build_opener',return_value=opener):
            out=profile.execute(self.root,r)
        self.assertTrue(out['ok']);self.assertNotIn(KEY,json.dumps(out));self.assertEqual(before,self.path.read_bytes())
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,profile.OFFICIAL+'/v1/messages')
        self.assertEqual(json.loads(req.data)['max_tokens'],64)
        self.assertIsNone(profile.NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid'))
    def test_http_error_never_exposes_server_body_or_key(self):
        r=self.request();r['operation']='test'
        opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://example.invalid',401,KEY,{},io.BytesIO(KEY.encode()))
        with patch('backend_profile.urllib.request.build_opener',return_value=opener):out=profile.execute(self.root,r)
        self.assertFalse(out['ok']);self.assertNotIn(KEY,json.dumps(out))

if __name__=='__main__':unittest.main()
