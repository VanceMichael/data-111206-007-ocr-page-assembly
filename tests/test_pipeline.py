import json
import tempfile
import unittest
from pathlib import Path

from pipeline import DISCARD, Pipeline
from raw_store import RawStore

BATCHES = json.loads(Path('fixtures/batches.json').read_text(encoding='utf-8'))
META = {page['page']: page for page in BATCHES['pages']}
NOW = '2026-11-12T00:00:00Z'


def alto(page_id, tokens, width=1000, height=1400):
    strings = ''.join(
        f'<ns0:String ID="{t["id"]}" CONTENT="{t["text"]}" HPOS="{t["box"][0]}" '
        f'VPOS="{t["box"][1]}" WIDTH="{t["box"][2]}" HEIGHT="{t["box"][3]}" '
        f'WC="{t.get("wc", 0.99)}" />' for t in tokens)
    return (f'<ns0:alto xmlns:ns0="http://www.loc.gov/standards/alto/ns-v4#">'
            f'<ns0:Layout><ns0:Page ID="{page_id}" WIDTH="{width}" HEIGHT="{height}">'
            f'<ns0:PrintSpace><ns0:TextBlock ID="b"><ns0:TextLine ID="l">{strings}'
            f'</ns0:TextLine></ns0:TextBlock></ns0:PrintSpace></ns0:Page>'
            f'</ns0:Layout></ns0:alto>').encode()


def token(identity, text, left, top, wc=0.99):
    return {'id': identity, 'text': text, 'box': [left, top, 300, 40], 'wc': wc}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.raw = RawStore(root / 'raw.sqlite3')
        self.pipe = Pipeline(self.raw, root / 'pipeline.sqlite3')

    def tearDown(self):
        self.pipe.close()
        self.raw.close()
        self.directory.cleanup()

    def ingest(self, *names):
        return [self.pipe.ingest_page(name, Path(META[name]['path']).read_bytes(),
                                      META[name], NOW) for name in names]

    def ingest_batch1(self):
        return self.ingest(*BATCHES['scan_order'])

    def rows_of(self, doc):
        return {row['名称']['值']: row for row in self.pipe.document(doc)['货物行']}

    def test_rotated_page_normalized_before_layout(self):
        self.ingest('b1')
        stored = json.loads(self.pipe.connection.execute(
            "SELECT tokens,width,height FROM pages WHERE logical_page='b1'").fetchone()[0])
        first = next(t for t in stored if t['text'] == '申报企业')
        self.assertEqual(first['frame'], [80, 100, 300, 40])
        rows = self.rows_of('SYN-B')
        self.assertEqual(rows['阀门']['关键值']['数量']['值'], '7')

    def test_out_of_order_attribution_and_conflict_pause(self):
        self.ingest_batch1()
        publication = self.pipe.publication()
        self.assertEqual([p['逻辑页'] for p in self.pipe.document('SYN-A')['页面']], ['a1', 'a2'])
        self.assertIn('SYN-B', publication['可发布'])
        self.assertIn('SYN-A', publication['暂停'])
        self.assertIn('SYN-C', publication['暂停'])
        held = {page['逻辑页']: page['原因'] for page in publication['搁置页']}
        self.assertEqual(held['conflict'], '二维码冲突')

    def test_cross_page_row_assembly_with_provenance(self):
        self.ingest_batch1()
        row = self.rows_of('SYN-A')['长名称部件上半长名称部件下半']
        self.assertTrue(row['跨页'])
        self.assertEqual(row['来源页'], ['a1', 'a2'])
        self.assertEqual(row['关键值']['数量']['值'], '8')
        pages = {ref['页'] for ref in row['名称']['证据']}
        self.assertEqual(pages, {'a1', 'a2'})

    def test_duplicate_scan_does_not_double_rows(self):
        receipts = self.ingest_batch1()
        duplicate = next(r for r in receipts if r['来源'] == 'a1-duplicate')
        self.assertEqual(duplicate['结果'], '重复扫描')
        self.assertEqual(list(self.rows_of('SYN-A').keys()).count('电机壳体'), 1)
        page = self.pipe.document('SYN-A')['页面'][0]
        self.assertEqual(page['重复扫描别名'], ['a1-duplicate'])

    def test_low_confidence_review_shows_evidence_and_candidates(self):
        self.ingest_batch1()
        item = next(i for i in self.pipe.review_queue() if i['type'] == '低置信关键值')
        self.assertEqual(item['field'], 'rows[控制器#1].数量')
        self.assertEqual([c['value'] for c in item['candidates']], ['l0', '10'])
        evidence = item['evidence']['相关词框'][0]
        self.assertEqual((evidence['page'], evidence['ocr_revision'], evidence['token']),
                         ('a2', '1', 'word-3-1'))

    def test_decision_binds_revision_and_not_applied_to_new_boxes(self):
        self.ingest_batch1()
        item = next(i for i in self.pipe.review_queue() if i['type'] == '低置信关键值')
        self.pipe.decide(item['key'], '10', '复核员甲', '按图确认', NOW)
        self.assertEqual(self.rows_of('SYN-A')['控制器']['关键值']['数量']['来源'],
                         {'人工决定': 1})
        before = self.rows_of('SYN-A')
        self.ingest('a2-new')
        after = self.rows_of('SYN-A')
        self.assertEqual(after['控制器']['关键值']['数量']['来源'], 'OCR')
        self.assertEqual(after['控制器']['关键值']['数量']['证据'][0]['识别版本'], '2')
        self.assertEqual(before['电机壳体'], after['电机壳体'])
        log = {entry['复核项']: entry['状态'] for entry in self.pipe.build()['决定日志']}
        self.assertEqual(log['lowconf|SYN-A|控制器#1|数量'], '未生效（词框已变化或已被取代）')
        old_digest = next(r['原文摘要'] for r in self.ingest_receipts('a2') )
        self.assertIn(b'word-3-1', self.raw.read(old_digest))

    def ingest_receipts(self, name):
        return [r for r in [self.pipe.ingest_page(name, Path(META[name]['path']).read_bytes(),
                                                  META[name], NOW)]]

    def test_rerecognition_still_low_confidence_reopens_item(self):
        self.ingest_batch1()
        item = next(i for i in self.pipe.review_queue() if i['type'] == '低置信关键值')
        self.pipe.decide(item['key'], '10', '复核员甲', '按图确认', NOW)
        body = Path(META['a2-new']['path']).read_bytes().replace(
            b'CONTENT="10" HPOS="600" VPOS="400" WIDTH="300" HEIGHT="40" WC="0.99"',
            b'CONTENT="10" HPOS="600" VPOS="400" WIDTH="300" HEIGHT="40" WC="0.50"')
        self.pipe.ingest_page('a2-new', body, META['a2-new'], NOW)
        queue = self.pipe.review_queue()
        reopened = next(i for i in queue if i['type'] == '低置信关键值')
        self.assertEqual(reopened['binding']['tokens'][0]['ocr_revision'], '2')
        self.assertEqual(self.rows_of('SYN-A')['控制器']['关键值']['数量']['来源'], 'OCR(待复核)')

    def test_publication_gating(self):
        self.ingest_batch1()
        paused = self.pipe.publication()['暂停']
        self.assertIn('SYN-A', paused)
        self.assertIn('SYN-C', paused)
        for item in self.pipe.review_queue():
            value = ('10' if item['type'] == '低置信关键值' else DISCARD)
            self.pipe.decide(item['key'], value, '复核员甲', '处理', NOW)
        publication = self.pipe.publication()
        self.assertIn('SYN-A', publication['可发布'])
        self.assertIn('SYN-C', publication['暂停'])
        self.assertIn('无归属页面', publication['暂停']['SYN-C'])

    def test_conflict_decision_can_attribute_page(self):
        self.ingest_batch1()
        item = next(i for i in self.pipe.review_queue() if i['type'] == '二维码冲突')
        self.pipe.decide(item['key'], '归入 SYN-C', '复核员甲', '人工判定归属', NOW)
        rows = self.rows_of('SYN-C')
        self.assertEqual(rows['混合资料']['关键值']['数量']['值'], '4')
        self.assertNotIn('conflict', {p['逻辑页'] for p in self.pipe.publication()['搁置页']})

    def test_header_numbers_never_become_quantities(self):
        body = alto('x1', [
            token('w0', '申报企业', 80, 100), token('w1', '合成丁', 600, 100),
            token('w2', '第3页', 600, 150),
            token('w3', '货物名称', 80, 200), token('w4', '数量', 600, 200),
            token('w5', '零件', 80, 300), token('w6', '5', 600, 300),
        ])
        meta = {'batch': 't', 'rotation': 0, 'barcodes': ['SYN-X'],
                'ocr_revision': '1', 'replaces': None}
        self.pipe.ingest_page('x1', body, meta, NOW)
        rows = self.rows_of('SYN-X')
        self.assertEqual(list(rows), ['零件'])
        self.assertEqual(rows['零件']['关键值']['数量']['值'], '5')

    def test_incomplete_row_pauses_document(self):
        body = alto('y1', [
            token('w0', '申报企业', 80, 100), token('w1', '合成戊', 600, 100),
            token('w3', '货物名称', 80, 200), token('w4', '数量', 600, 200),
            token('w5', '半成品', 80, 300),
        ])
        meta = {'batch': 't', 'rotation': 0, 'barcodes': ['SYN-Y'],
                'ocr_revision': '1', 'replaces': None}
        self.pipe.ingest_page('y1', body, meta, NOW)
        publication = self.pipe.publication()
        self.assertIn('存在未闭合货物行', publication['暂停']['SYN-Y'])
        self.assertEqual(self.pipe.document('SYN-Y')['未闭合行'][0]['名称'], '半成品')

    def test_barcodeless_page_without_evidence_is_held(self):
        body = alto('z1', [
            token('w3', '货物名称', 80, 200), token('w4', '数量', 600, 200),
            token('w5', '零件', 80, 300), token('w6', '5', 600, 300),
        ])
        meta = {'batch': 't', 'rotation': 0, 'barcodes': [],
                'ocr_revision': '1', 'replaces': None}
        self.pipe.ingest_page('z1', body, meta, NOW)
        held = {page['逻辑页']: page['原因'] for page in self.pipe.publication()['搁置页']}
        self.assertEqual(held['z1'], '归属证据不足')
        self.assertIn('归属证据不足', {i['type'] for i in self.pipe.review_queue()})

    def test_changed_source_content_rejected(self):
        self.ingest('a1')
        body = Path(META['a1']['path']).read_bytes().replace('电机壳体'.encode(), '改动内容'.encode())
        with self.assertRaises(ValueError):
            self.pipe.ingest_page('a1', body, META['a1'], NOW)

    def test_ingest_order_does_not_change_result(self):
        self.ingest_batch1()
        first = self.pipe.document('SYN-A')
        other_dir = tempfile.TemporaryDirectory()
        other = Pipeline(RawStore(Path(other_dir.name) / 'raw.sqlite3'),
                         Path(other_dir.name) / 'pipeline.sqlite3')
        try:
            for name in reversed(BATCHES['scan_order']):
                other.ingest_page(name, Path(META[name]['path']).read_bytes(), META[name], NOW)
            second = other.document('SYN-A')
        finally:
            other.close()
            other_dir.cleanup()
        simplify = lambda doc: [(r['名称']['值'], {c: k['值'] for c, k in r['关键值'].items()})
                                for r in doc['货物行']]
        self.assertEqual(simplify(first), simplify(second))

    def test_unknown_item_rejected(self):
        self.ingest_batch1()
        with self.assertRaises(KeyError):
            self.pipe.decide('lowconf|不存在', '1', '复核员甲', '', NOW)


if __name__ == '__main__':
    unittest.main()
