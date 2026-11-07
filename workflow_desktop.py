"""Bounded JSON client for the desktop workflow control channel."""
import argparse
import json
from pathlib import Path
import sys
from template_desktop import request_templates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raw = sys.stdin.buffer.read(80 * 1024 + 1)
        if len(raw) > 80 * 1024:
            raise ValueError()
        request = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(request, dict):
            raise ValueError()
        result = request_templates(args.root.resolve(), request, workflow=True)
    except Exception:
        result = dict(ok=False, message='操作未能确认，请重新读取后核对。')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
