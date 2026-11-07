"""Windows current-user DPAPI storage, with read-only legacy plaintext support."""
import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import uuid

MAGIC=b'WX-DPAPI-1\n'

class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data,decrypt=False,label='WeChat Assistant API key'):
    if os.name!='nt':raise OSError('密钥加密需要 Windows 当前用户环境')
    raw=(ctypes.c_ubyte*len(data)).from_buffer_copy(data)
    source=Blob(len(data),ctypes.cast(raw,ctypes.POINTER(ctypes.c_ubyte)));output=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        fn=crypt.CryptUnprotectData
        fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output))
    else:
        fn=crypt.CryptProtectData
        fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        ok=fn(ctypes.byref(source),label,None,None,None,1,ctypes.byref(output))
    if not ok:raise OSError('Windows 密钥%s失败（代码 %d）'%('解密' if decrypt else '加密',ctypes.get_last_error()))
    try:return ctypes.string_at(output.data,output.size)
    finally:kernel.LocalFree(output.data)


def read_secret(path):
    raw=Path(path).read_bytes()
    if raw.startswith(MAGIC):
        try:payload=base64.b64decode(raw[len(MAGIC):],validate=True)
        except ValueError:raise OSError('加密密钥文件格式损坏') from None
        try:return _crypt(payload,True).decode('utf-8').strip()
        except UnicodeError:raise OSError('加密密钥文件内容无效') from None
    try:return raw.decode('utf-8-sig').strip()
    except UnicodeError:raise OSError('密钥文件格式无效') from None


def is_encrypted(path):
    try:return Path(path).read_bytes().startswith(MAGIC)
    except OSError:return False


def write_secret(path,value,exclusive=False):
    path=Path(path)
    if path.suffix.lower()!='.key':raise OSError('密钥文件必须使用 .key 扩展名')
    encoded=MAGIC+base64.b64encode(_crypt(value.encode('utf-8')))
    if exclusive:
        with path.open('xb') as out:out.write(encoded);out.flush();os.fsync(out.fileno())
        if read_secret(path)!=value:raise OSError('加密密钥回读校验失败')
        return
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as out:out.write(encoded);out.flush();os.fsync(out.fileno())
        if read_secret(temp)!=value:raise OSError('加密密钥回读校验失败')
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


def protect_text(value):
    """把一段文本加密成能直接放进 JSON 字符串的密文（不依赖 .key 文件）。

    和 read_secret/write_secret 用同一个 MAGIC 与同一个 DPAPI 保护，区别只是
    结果落在 JSON 字段里而不是独立文件——config.json 的 bot_token 用它。
    """
    return (MAGIC+base64.b64encode(_crypt(value.encode('utf-8'),label='WeChat Assistant bot token'))).decode('ascii')


def is_protected(text):
    """这段文本是不是 protect_text 的产物（读配置时用来区分明文/密文）。"""
    return isinstance(text,str) and text.startswith(MAGIC.decode('ascii'))


def unprotect_text(text):
    """解开 protect_text 的产物。明文原样返回，旧配置的写法照样读得进。"""
    if not is_protected(text):return text
    try:payload=base64.b64decode(text[len(MAGIC):],validate=True)
    except ValueError:raise OSError('加密凭据格式损坏') from None
    try:return _crypt(payload,True).decode('utf-8')
    except UnicodeError:raise OSError('加密凭据内容无效') from None


def migrate_secret(path):
    path=Path(path);original=path.read_bytes()
    if original.startswith(MAGIC):read_secret(path);return False
    value=read_secret(path)
    encoded=MAGIC+base64.b64encode(_crypt(value.encode('utf-8')))
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_bytes(encoded)
        if read_secret(temp)!=value:raise OSError('迁移回读校验失败')
        if path.read_bytes()!=original:raise OSError('密钥文件已被修改，未覆盖')
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)
    return True
