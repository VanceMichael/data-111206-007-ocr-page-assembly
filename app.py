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


def _compact(pipe):
    publication = pipe.publication()
    queue = pipe.review_queue()
    return {'可发布': publication['可发布'],
            '暂停': publication['暂停'],
            '搁置页': [{k: page[k] for k in ('逻辑页', '原因')} for page in publication['搁置页']],
            '待决复核项': [{'复核项': item['key'], '类型': item['type'],
                            '候选': [c['value'] for c in item['candidates']]}
                           for item in queue]}


def _row_changes(before, after):
    former = {row['名称']['值']: row for row in before['货物行']}
    latter = {row['名称']['值']: row for row in after['货物行']}
    changes = []
    for name in former.keys() & latter.keys():
        same = json.dumps(former[name], ensure_ascii=False, sort_keys=True) == \
               json.dumps(latter[name], ensure_ascii=False, sort_keys=True)
        entry = {'名称': name, '行证据未变': same}
        if not same:
            entry['前数量'] = former[name]['关键值'].get('数量', {}).get('值')
            entry['后数量'] = latter[name]['关键值'].get('数量', {}).get('值')
            entry['后来源'] = latter[name]['关键值'].get('数量', {}).get('来源')
        changes.append(entry)
    return changes


def pipeline_demo(store, db_path):
    """乱序页、跨页断行与重识别：哪些文档可发布、哪些还欠证据。"""
    from pipeline import Pipeline, DISCARD
    pipe = Pipeline(store, db_path)
    try:
        batches = json.loads(Path('fixtures/batches.json').read_text(encoding='utf-8'))
        meta = {page['page']: page for page in batches['pages']}

        def ingest(names, seen_at):
            return [pipe.ingest_page(name, Path(meta[name]['path']).read_bytes(), meta[name], seen_at)
                    for name in names]

        out = {}
        t1 = datetime.now(timezone.utc).isoformat()
        out['阶段一_按扫描顺序接收'] = ingest(batches['scan_order'], t1)
        out['阶段一_构建'] = _compact(pipe)

        t2 = datetime.now(timezone.utc).isoformat()
        decisions = []
        for item in pipe.review_queue():
            if item['type'] == '低置信关键值':
                guess = next((c['value'] for c in item['candidates'] if c['kind'] == '形近字猜测'),
                             item['candidates'][0]['value'])
                decisions.append(pipe.decide(item['key'], guess, '复核员甲',
                                             '对照扫描图确认数量', t2))
            elif item['type'] == '二维码冲突':
                decisions.append(pipe.decide(item['key'], DISCARD, '复核员甲',
                                             '混扫废页，不凭文件名补齐', t2))
        out['阶段二_复核决定'] = decisions
        out['阶段二_构建'] = _compact(pipe)
        before = pipe.document('SYN-A')

        t3 = datetime.now(timezone.utc).isoformat()
        out['阶段三_重识别页到达'] = ingest(batches['updated_pages'], t3)
        after = pipe.document('SYN-A')
        out['阶段三_行级影响'] = _row_changes(before, after)
        out['阶段三_构建'] = _compact(pipe)

        digests = {}
        logical_pages = {page['逻辑页'] for page in after['页面']}
        for entry in pipe.all_revisions():
            if entry['逻辑页'] in logical_pages:
                digests[f"{entry['逻辑页']}@{entry['识别版本']}"] = {
                    '原文摘要': entry['原文摘要'],
                    '原文长度': len(store.read(entry['原文摘要']))}
        out['原始OCR各版本仍可读取'] = digests
        out['决定日志'] = pipe.build()['决定日志']
        out['最终发布'] = pipe.publication()
        out['文档明细'] = {doc: pipe.document(doc) for doc in ('SYN-A', 'SYN-B', 'SYN-C')}
        return out
    finally:
        pipe.close()


def main():
    parser = argparse.ArgumentParser(description='本地原文采集演示')
    parser.add_argument('command', choices=['demo', 'pipeline'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    parser.add_argument('--db', default='output/pipeline.sqlite3')
    args = parser.parse_args()
    store = RawStore(args.output)
    try:
        if args.command == 'pipeline':
            result = pipeline_demo(store, args.db)
        else:
            result = demo(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
