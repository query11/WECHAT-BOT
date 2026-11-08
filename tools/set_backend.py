"""Change the saved provider only; never starts or restarts the bridge."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "app"))
from deepseek_runner import MODELS
import api_profiles
from key_vault import read_secret


def set_backend(backend, config_path=None):
    backend = str(backend).lower()
    path = Path(config_path) if config_path is not None else PROJECT / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        raise ValueError("现有配置无法读取或不是合法 JSON；未修改") from None
    if not isinstance(config, dict):
        raise ValueError("现有配置必须是 JSON 对象；未修改")
    if backend not in api_profiles.backend_ids(config):
        raise ValueError("后端只能是 claude、deepseek 或 api_profiles 中已保存的接口 id；未修改")
    if backend not in api_profiles.RESERVED:
        profile = api_profiles.find(config, backend)
        key_path = path.resolve().parent / str(profile.get("key_file") or "")
        if not profile.get("key_file") or key_path.suffix.lower() != ".key":
            raise ValueError("%s 尚未配置 API Key；未修改" % profile["name"])
        try:
            key = read_secret(key_path)
        except (OSError, UnicodeError, TypeError, ValueError):
            raise ValueError("%s 密钥未配置或无法读取；未修改" % profile["name"]) from None
        if not isinstance(key, str) or not key or any(c.isspace() for c in key):
            raise ValueError("%s 密钥格式无效；未修改" % profile["name"])
    if backend == "deepseek":
        options = config.get("deepseek") or {}
        if not isinstance(options, dict) or options.get("model", "deepseek-flash") not in MODELS:
            raise ValueError("DeepSeek 模型配置无效；未修改")
        try:
            key_path = path.resolve().parent / options.get("key_file", ".runtime/deepseek-api.key")
        except (TypeError, ValueError):
            raise ValueError("DeepSeek 密钥文件路径无效；未修改") from None
        if key_path.suffix.lower() != ".key":
            raise ValueError("DeepSeek 密钥文件必须使用 .key 扩展名；未修改")
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            try:
                key = read_secret(key_path)
            except (OSError, UnicodeError, TypeError, ValueError):
                raise ValueError("DeepSeek 密钥未配置或无法读取；未修改") from None
        if not isinstance(key, str) or not key.startswith("sk-") or any(c.isspace() for c in key):
            raise ValueError("DeepSeek 密钥格式无效；未修改")
    config["backend"] = backend
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", dir=path.parent,
                                         prefix=".backend-", suffix=".tmp", delete=False) as out:
            temp_path = out.name
            json.dump(config, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_path, path)
    except OSError:
        raise ValueError("配置写入失败；原配置保留") from None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", help="claude、deepseek 或 api_profiles 中已保存的接口 id")
    parser.add_argument("--config", help="现有配置文件路径")
    args = parser.parse_args(argv)
    try:
        set_backend(args.backend, args.config)
    except ValueError as exc:
        print("❌ " + str(exc))
        return 1
    print("✅ 已保存后端选择：%s。后台尚未重启；空闲时 /reload 或正常重启后生效。" % args.backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
