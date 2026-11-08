"""Private profile editor service. Requests use stdin; responses never contain keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from key_vault import read_secret,write_secret,is_encrypted
import api_profiles
from api_profiles import ProfileError,validate_url

OFFICIAL = 'https://api.deepseek.com/anthropic'
MODELS = ('deepseek-flash', 'deepseek-v4-pro')


def read_config(root):
    try:
        blob=(Path(root)/'config.json').read_bytes()
        raw=json.loads(blob.decode('utf-8-sig'))
        if not isinstance(raw,dict):raise ValueError()
        return raw,blob,hashlib.sha256(blob).hexdigest()
    except (OSError,ValueError):raise ProfileError('无法读取本机配置文件') from None


def key_value(root,provider,profile):
    if provider=='deepseek' and os.environ.get('DEEPSEEK_API_KEY'):
        return os.environ['DEEPSEEK_API_KEY']
    name=profile.get('key_file', '.runtime/deepseek-api.key' if provider=='deepseek' else '')
    if not name:return ''
    try:
        path=Path(root)/name
        if path.suffix.lower()!='.key':raise ProfileError('密钥文件必须使用 .key 扩展名')
        return read_secret(path)
    except OSError:return ''


def view(root):
    raw,_,revision=read_config(root)
    ds=raw.get('deepseek') or {};cl=raw.get('claude_api') or {}
    return {'ok':True,'revision':revision,'active_backend':raw.get('backend','claude'),
        'deepseek':{'model':ds.get('model','deepseek-flash'),'base_url':OFFICIAL,'enabled':True,
                    'configured':bool(key_value(root,'deepseek',ds)),
                    'env_override':bool(os.environ.get('DEEPSEEK_API_KEY')),
                    'encrypted':is_encrypted(Path(root)/ds.get('key_file','.runtime/deepseek-api.key'))},
        'claude':{'model':cl.get('model',''),'base_url':cl.get('base_url','https://api.anthropic.com'),
                  'enabled':bool(cl.get('enabled',False)),
                  'configured':bool(key_value(root,'claude',cl)),
                  'encrypted':is_encrypted(Path(root)/cl.get('key_file',''))},
        'profiles':[_profile_view(root,p) for p in api_profiles.profiles(raw)],
        'presets':[dict(p,models=list(p['models'])) for p in api_profiles.PRESETS],
        'max_profiles':api_profiles.MAX_PROFILES}


def _profile_view(root,p):
    key_file=p.get('key_file','')
    return {'id':p['id'],'name':p['name'],'preset':p.get('preset','custom'),'base_url':p['base_url'],
            'model':p['model'],'vision':bool(p.get('vision')),'configured':bool(key_value(root,'profile',p)),
            'encrypted':bool(key_file) and is_encrypted(Path(root)/key_file)}


def _prepare_profile(raw,request):
    """Validate one saved service; returns (('profile', index or None), profile, old)."""
    items=raw.get('api_profiles') or []
    if not isinstance(items,list):raise ProfileError('本机接口配置格式无效，请检查 config.json')
    pid=str(request.get('profile_id') or '')
    index=next((i for i,e in enumerate(items) if isinstance(e,dict) and e.get('id')==pid),None) if pid else None
    if pid and index is None:raise ProfileError('该接口已被删除，请重新打开配置页')
    old=dict(items[index]) if index is not None else {}
    kind=old.get('preset') or request.get('preset') or 'custom'
    if kind!='custom' and kind not in api_profiles.PRESET_IDS:raise ProfileError('请选择有效的服务类型')
    saved=api_profiles.profiles(raw)
    if index is None and len(saved)>=api_profiles.MAX_PROFILES:
        raise ProfileError('最多保存 %d 个接口' % api_profiles.MAX_PROFILES)
    name=api_profiles.clean_name(request.get('name') or (api_profiles.preset(kind) or {}).get('name',''))
    taken={p['name'].casefold() for p in saved if p['id']!=pid}|{n.casefold() for n in api_profiles.BUILTIN_NAMES.values()}
    if name.casefold() in taken:raise ProfileError('已有同名后端，请换一个名称')
    profile=dict(old,preset=kind,name=name,base_url=validate_url(request.get('base_url','')),
                 model=api_profiles.clean_model(request.get('model')),vision=bool(request.get('vision')))
    profile['id']=old.get('id') or api_profiles.new_id(raw,kind)
    return ('profile',index),profile,old


def prepare(root,request):
    raw,blob,revision=read_config(root)
    provider=request.get('provider')
    if provider=='profile':
        field,profile,old=_prepare_profile(raw,request)
        key=request.get('api_key','')
        if not isinstance(key,str):raise ProfileError('API Key 格式无效')
        actual=key.strip() or key_value(root,'profile',old)
        if not actual or len(actual)>4096 or any(c.isspace() for c in actual):
            raise ProfileError('请填写 API Key；留空只能保留已配置的密钥')
        return raw,blob,revision,field,profile,actual
    if provider not in ('deepseek','claude'):raise ProfileError('请选择要配置的后端')
    field='deepseek' if provider=='deepseek' else 'claude_api'
    old=raw.get(field) or {}
    profile=dict(old)
    enabled=True if provider=='deepseek' else bool(request.get('enabled',False))
    if provider=='claude':profile['enabled']=enabled
    key=request.get('api_key','')
    if not isinstance(key,str):raise ProfileError('API Key 格式无效')
    key=key.strip()
    if not enabled:return raw,blob,revision,field,profile,''
    model=str(request.get('model','')).strip()
    if not model or len(model)>160 or not re.fullmatch(r'[A-Za-z0-9_./:\[\]-]+',model):
        raise ProfileError('请填写有效的模型名称')
    if provider=='deepseek':
        if model not in MODELS:raise ProfileError('请选择 DeepSeek 官方模型')
        if os.environ.get('DEEPSEEK_API_KEY') and key:
            raise ProfileError('当前使用 DEEPSEEK_API_KEY 环境变量，请先移除该变量再在界面更换密钥')
    else:profile['base_url']=validate_url(request.get('base_url',''))
    profile['model']=model
    actual=key or key_value(root,provider,old)
    if not actual or len(actual)>4096 or any(c.isspace() for c in actual):
        raise ProfileError('请填写 API Key；留空只能保留已配置的密钥')
    if provider=='deepseek' and not actual.startswith('sk-'):raise ProfileError('DeepSeek API Key 应以 sk- 开头')
    return raw,blob,revision,field,profile,actual


def _write_config(root,raw,blob):
    """Atomically replaces config.json unless another program changed it since ``blob``."""
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8-sig',dir=root,
                prefix='.profile-',suffix='.tmp',delete=False) as f:
            temp=Path(f.name);json.dump(raw,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        if (root/'config.json').read_bytes()!=blob:raise ProfileError('配置已被其他程序修改，未覆盖')
        os.replace(temp,root/'config.json');temp=None
    finally:
        if temp is not None:
            try:temp.unlink(missing_ok=True)
            except OSError:pass


def save(root,request):
    root=Path(root);raw,blob,revision,field,profile,key=prepare(root,request)
    if request.get('revision')!=revision:raise ProfileError('配置已被其他窗口修改，请重新打开配置页')
    key_path=None
    try:
        if request.get('api_key','').strip() and key:
            folder=root/'.runtime';folder.mkdir(exist_ok=True)
            prefix=request['provider'] if isinstance(field,str) else 'api-'+profile['id']
            key_path=folder/(prefix+'-'+uuid.uuid4().hex+'.key')
            write_secret(key_path,key,exclusive=True)
            profile['key_file']=key_path.relative_to(root).as_posix()
        if isinstance(field,tuple):
            items=list(raw.get('api_profiles') or [])
            if field[1] is None:items.append(profile)
            else:items[field[1]]=profile
            raw['api_profiles']=items
        else:raw[field]=profile
        _write_config(root,raw,blob);key_path=None
    except OSError:raise ProfileError('保存失败，原配置保持不变') from None
    finally:
        if key_path is not None:
            try:key_path.unlink(missing_ok=True)
            except OSError:pass
    return dict(view(root),message='配置已保存',saved=True,saved_id=profile.get('id',request['provider']))


def delete(root,request):
    """Removes one saved service and then its key file; the active backend cannot be removed."""
    root=Path(root);raw,blob,revision=read_config(root)
    if request.get('revision')!=revision:raise ProfileError('配置已被其他窗口修改，请重新打开配置页')
    items=raw.get('api_profiles') or []
    pid=str(request.get('profile_id') or '')
    index=next((i for i,e in enumerate(items) if isinstance(e,dict) and e.get('id')==pid),None) if isinstance(items,list) and pid else None
    if index is None:raise ProfileError('该接口已不存在，请重新打开配置页')
    if str(raw.get('backend','claude')).lower()==pid:raise ProfileError('正在使用的后端不能删除，请先切换到其他后端')
    key_file=str(items[index].get('key_file') or '')
    raw['api_profiles']=[e for i,e in enumerate(items) if i!=index]
    try:_write_config(root,raw,blob)
    except OSError:raise ProfileError('删除失败，原配置保持不变') from None
    runtime=(root/'.runtime').resolve()
    try:
        path=(root/key_file).resolve()
        if key_file and path.suffix.lower()=='.key' and path.parent==runtime:path.unlink(missing_ok=True)
    except OSError:pass
    return dict(view(root),message='已删除接口',deleted=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def test_connection(root,request):
    _,_,_,field,profile,key=prepare(root,request)
    if field=='claude_api' and not profile.get('enabled'):
        raise ProfileError('沿用原 Claude 配置时无需在此测试；启用自定义 API 后可测试')
    base=OFFICIAL if field=='deepseek' else profile['base_url']
    url=base+('/messages' if base.endswith('/v1') else '/v1/messages')
    body={'model':profile['model'],'max_tokens':64,'messages':[{'role':'user','content':'Reply exactly CONNECTION_OK'}]}
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={
        'Content-Type':'application/json','x-api-key':key,'Authorization':'Bearer '+key,'anthropic-version':'2023-06-01'})
    try:
        opener=urllib.request.build_opener(NoRedirect())
        with opener.open(req,timeout=20) as response:
            data=json.loads(response.read(1024*1024))
        blocks=data.get('content')
        if not isinstance(blocks,list) or not any(b.get('type')=='text' and b.get('text') for b in blocks if isinstance(b,dict)):
            raise ProfileError('接口已响应，但没有返回兼容的文本内容')
    except urllib.error.HTTPError as e:
        e.close()
        message={401:'密钥无效或已过期',403:'接口拒绝访问，请检查权限',402:'账户余额不足',429:'请求过于频繁，请稍后再试',404:'接口路径或模型不存在'}
        raise ProfileError(message.get(e.code,'接口暂不可用（HTTP %d）'%e.code)) from None
    except (OSError,ValueError,urllib.error.URLError):
        raise ProfileError('连接失败或响应格式不兼容，请检查地址与网络') from None
    return {'ok':True,'message':'连接测试通过；仅验证模型接口，尚未保存配置'}


def execute(root,request):
    try:
        if not isinstance(request,dict):raise ProfileError('请求格式无效')
        op=request.get('operation')
        if op=='view':return view(root)
        if op=='save':return save(root,request)
        if op=='test':return test_connection(root,request)
        if op=='delete':return delete(root,request)
        raise ProfileError('不支持的操作')
    except (ProfileError,OSError,ValueError,TypeError,AttributeError):
        # Only known safe, locally-authored validation messages may be returned.
        exc=sys.exc_info()[1]
        return {'ok':False,'message':str(exc) if isinstance(exc,ProfileError) else '配置操作失败，请检查本机配置'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parent.parent));a=parser.parse_args()
    try:request=json.loads(sys.stdin.read(32768).lstrip('\ufeff'))
    except ValueError:request=None
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(execute(a.root,request),ensure_ascii=False))

if __name__=='__main__':main()
