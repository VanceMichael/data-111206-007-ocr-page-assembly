import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from raw_store import RawStore


def demo(store):
    observed_at = datetime.now(timezone.utc).isoformat()
    from alto_source import words
    result=[]
    for path in sorted(Path('fixtures/pages').glob('*.xml')):
        raw=path.read_bytes()
        pages=words(raw)
        digest=store.save(path.name,raw,{'page_count':len(pages)},observed_at)
        result.append({'文件':path.name,'词框数':sum(len(page['tokens']) for page in pages),'低置信词框':[token['id'] for page in pages for token in page['tokens'] if token['confidence'] is not None and token['confidence']<0.8],'原文摘要':digest})
    return {'页面观察':result,'说明':'保留原始词框，不推断页面归属'}


def main():
    parser = argparse.ArgumentParser(description='本地原文采集演示')
    parser.add_argument('command', choices=['demo'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        result = demo(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
