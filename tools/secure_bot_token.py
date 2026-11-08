"""Move a plaintext config bot_token into Windows DPAPI storage.

Only the bot_token value changes; other fields are preserved and JSON is written
with a UTF-8 BOM. The temporary file is verified before replacing an unchanged
original. Running it again after encryption is a no-op. Never restarts the bridge.
"""
import argparse
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "app"))
from bridge_config import make_console_safe, secure_bot_token


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="配置文件路径（默认用项目里的 config.json）")
    args = parser.parse_args(argv)
    # 中文 Windows 的控制台是 GBK：不换成「替换」的话，下面的记号会直接抛
    # UnicodeEncodeError（和 bridge_config.make_console_safe 里记的是同一件事）。
    make_console_safe()
    try:
        changed, message = secure_bot_token(args.config or str(PROJECT / "config.json"))
    except (SystemExit, OSError) as exc:
        print("❌ " + str(exc))
        return 1
    print(("✅ " if changed else "ℹ ") + message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
