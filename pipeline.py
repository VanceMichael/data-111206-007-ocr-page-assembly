"""词框之后的资料采集流程：页面归属、货物行组装与人工复核。

输入为 ALTO 词框、页面尺寸、扫描批次与二维码识别结果。二维码只作为文档
候选；归属证据不足或二维码冲突时暂停相应文档，不凭文件名补齐。每个输出
字段都能指回原页词框（逻辑页、识别版本、词框 ID、原文摘要）。人工修订
绑定具体 OCR 输入版本，更换识别结果后旧决定不被默默套用到新词框。
"""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import alto_source

LOW_CONFIDENCE = 0.8
NAME_HEADER = '货物名称'
KEY_HEADERS = ('数量', '金额')
ENTERPRISE_MARK = '申报企业'
CONTINUATION_MARK = '续页'
ROTATIONS = (0, 90, 180, 270)
NUMBER = re.compile(r'^-?\d+(\.\d+)?$')
CONFUSABLE = str.maketrans({'l': '1', 'I': '1', 'O': '0', 'o': '0', 'S': '5', 'B': '8'})
DISCARD = '不归入任何文档'
JOIN_PREFIX = '归入 '


def rotate_frame(box, rotation, width, height):
    """把扫描坐标系中的词框换算到端正页面坐标系。

    rotation 表示扫描件需要逆时针旋转的角度。返回 (端正词框, 端正宽, 端正高)。
    """
    left, top, w, h = box
    if rotation == 0:
        return [left, top, w, h], width, height
    if rotation == 90:
        return [top, width - left - w, h, w], height, width
    if rotation == 180:
        return [width - left - w, height - top - h, w, h], width, height
    if rotation == 270:
        return [height - top - h, left, h, w], height, width
    raise ValueError('旋转角度只支持 0/90/180/270')


def build_lines(tokens):
    """按词框纵向重叠把词框聚成行，不按固定 y 坐标切。"""
    ordered = sorted(tokens, key=lambda t: (t['frame'][1], t['frame'][0]))
    lines = []
    for token in ordered:
        top = token['frame'][1]
        bottom = top + token['frame'][3]
        if lines:
            last = lines[-1]
            overlap = min(bottom, last['bottom']) - max(top, last['top'])
            shortest = min(bottom - top, last['bottom'] - last['top'])
            if overlap > 0 and overlap * 2 >= shortest:
                last['tokens'].append(token)
                last['top'] = min(last['top'], top)
                last['bottom'] = max(last['bottom'], bottom)
                continue
        lines.append({'top': top, 'bottom': bottom, 'tokens': [token]})
    for line in lines:
        line['tokens'].sort(key=lambda t: t['frame'][0])
    return lines


def column_regions(header_line):
    """从本页表头行的词框位置推出列区域；缺关键列表头则返回 None。"""
    anchors = [t for t in header_line['tokens'] if t['text'] == NAME_HEADER or t['text'] in KEY_HEADERS]
    anchors.sort(key=lambda t: t['frame'][0])
    texts = [t['text'] for t in anchors]
    if NAME_HEADER not in texts or not any(text in KEY_HEADERS for text in texts):
        return None
    bounds = []
    for left, right in zip(anchors, anchors[1:]):
        bounds.append((left['frame'][0] + left['frame'][2] + right['frame'][0]) / 2)
    regions = []
    for index, token in enumerate(anchors):
        low = bounds[index - 1] if index else float('-inf')
        high = bounds[index] if index < len(bounds) else float('inf')
        regions.append({'name': token['text'], 'low': low, 'high': high})
    return regions


def row_from_line(line, regions):
    """把一行词框按列区域归位；行内没有任何列词框时返回 None。"""
    fields = {region['name']: [] for region in regions}
    for token in line['tokens']:
        center = token['frame'][0] + token['frame'][2] / 2
        for region in regions:
            if region['low'] <= center < region['high']:
                fields[region['name']].append(token)
                break
    name_tokens = fields.get(NAME_HEADER, [])
    keys = {}
    for region in regions:
        if region['name'] == NAME_HEADER:
            continue
        tokens = fields[region['name']]
        keys[region['name']] = {'text': ''.join(t['text'] for t in tokens), 'tokens': tokens}
    if not name_tokens and all(not key['tokens'] for key in keys.values()):
        return None
    return {
        'name_text': ''.join(t['text'] for t in name_tokens),
        'name_tokens': name_tokens,
        'keys': keys,
        'line_tokens': list(line['tokens']),
    }


def page_evidence(header_lines):
    """从表头以上的页眉行提取主体证据与页类；页眉数字不会进入货物行。"""
    enterprises = []
    kind = 'unknown'
    evidence_tokens = []
    for line in header_lines:
        texts = [t['text'] for t in line['tokens']]
        if ENTERPRISE_MARK in texts:
            mark, line_kind = ENTERPRISE_MARK, 'start'
        elif CONTINUATION_MARK in texts:
            mark, line_kind = CONTINUATION_MARK, 'continuation'
        else:
            continue
        rest = [t for t in line['tokens'] if t['text'] != mark]
        if rest:
            enterprises.append(''.join(t['text'] for t in rest))
            if not evidence_tokens:
                evidence_tokens = list(line['tokens'])
        if line_kind == 'start' or kind == 'unknown':
            kind = line_kind
    conflict = len(set(enterprises)) > 1
    enterprise = enterprises[0] if enterprises and not conflict else None
    return enterprise, evidence_tokens, kind, conflict


def merge_rows(open_row, next_row):
    """把跨页/跨行断开的两段拼成一行，证据来自两段词框。"""
    keys = {}
    conflicts = []
    columns = list(open_row['keys']) + [c for c in next_row['keys'] if c not in open_row['keys']]
    for column in columns:
        former = open_row['keys'].get(column, {'text': '', 'tokens': []})
        latter = next_row['keys'].get(column, {'text': '', 'tokens': []})
        if former['text'] and latter['text']:
            conflicts.append((column, former, latter))
            keys[column] = {'text': former['text'], 'tokens': former['tokens'] + latter['tokens']}
        elif former['text'] or latter['text']:
            chosen = former if former['text'] else latter
            keys[column] = {'text': chosen['text'], 'tokens': chosen['tokens']}
        else:
            keys[column] = {'text': '', 'tokens': []}
    return {
        'name_text': open_row['name_text'] + next_row['name_text'],
        'name_tokens': open_row['name_tokens'] + next_row['name_tokens'],
        'keys': keys,
        'line_tokens': open_row['line_tokens'] + next_row['line_tokens'],
        'key_conflicts': conflicts,
    }


def tag_row(page, row):
    """给行内每个词框打上所属逻辑页，跨页合并后仍能指回原页。"""
    def tag(tokens):
        return [dict(token, page=page['logical']) for token in tokens]
    return {
        'name_text': row['name_text'],
        'name_tokens': tag(row['name_tokens']),
        'keys': {column: {'text': key['text'], 'tokens': tag(key['tokens'])}
                 for column, key in row['keys'].items()},
        'line_tokens': tag(row['line_tokens']),
    }


class Pipeline:
    """从页接收到文档结构化结果与复核决定的可调用路径。"""

    def __init__(self, raw_store, db_path):
        self.raw_store = raw_store
        if db_path != ':memory:':
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(db_path, timeout=10)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY,
                logical_page TEXT NOT NULL,
                ocr_revision TEXT NOT NULL,
                source TEXT NOT NULL UNIQUE,
                batch TEXT NOT NULL,
                rotation INTEGER NOT NULL,
                barcodes TEXT NOT NULL,
                digest TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                tokens TEXT NOT NULL,
                received_at TEXT NOT NULL,
                UNIQUE(logical_page, ocr_revision));
            CREATE TABLE IF NOT EXISTS duplicates (
                source TEXT PRIMARY KEY,
                page_id INTEGER NOT NULL REFERENCES pages(id),
                digest TEXT NOT NULL,
                barcodes TEXT NOT NULL,
                received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                item_key TEXT NOT NULL,
                type TEXT NOT NULL,
                documents TEXT NOT NULL,
                value TEXT NOT NULL,
                bound TEXT NOT NULL,
                reviewer TEXT NOT NULL,
                note TEXT NOT NULL,
                decided_at TEXT NOT NULL);
        """)

    # ---------- 页接收 ----------

    def ingest_page(self, source, body, meta, seen_at):
        """接收一页：校验批次元数据、归一化词框、识别重复扫描并保存原文。"""
        if not source or not isinstance(body, bytes) or not seen_at:
            raise ValueError('来源、原始字节和接收时间不可缺少')
        batch = meta.get('batch')
        rotation = meta.get('rotation', 0)
        barcodes = meta.get('barcodes') or []
        revision = str(meta.get('ocr_revision', ''))
        replaces = meta.get('replaces')
        if not batch:
            raise ValueError('扫描批次不可缺少')
        if rotation not in ROTATIONS:
            raise ValueError('旋转角度只支持 0/90/180/270')
        if not all(isinstance(code, str) and code for code in barcodes):
            raise ValueError('二维码必须是非空字符串')
        if not revision.isdigit():
            raise ValueError('识别版本必须是非负整数')
        if replaces is not None and (not isinstance(replaces, str) or not replaces or replaces == source):
            raise ValueError('replaces 必须指向另一个逻辑页')
        parsed = alto_source.words(body)
        if len(parsed) != 1:
            raise ValueError('每份输入必须恰好包含一页')
        raw_page = parsed[0]
        tokens = []
        width = height = None
        for token in raw_page['tokens']:
            frame, width, height = rotate_frame(token['box'], rotation, raw_page['width'], raw_page['height'])
            tokens.append({'id': token['id'], 'text': token['text'], 'box': token['box'],
                           'frame': frame, 'confidence': token['confidence']})
        fingerprint = hashlib.sha256(json.dumps(
            {'size': [width, height], 'tokens': [[t['text'], t['frame'], t['confidence']] for t in tokens]},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        digest = hashlib.sha256(body).hexdigest()
        logical = replaces or source

        existing = self.connection.execute(
            'SELECT id,digest FROM pages WHERE source=?', (source,)).fetchone()
        if existing is not None:
            if existing[1] != digest:
                raise ValueError('同一来源出现不同内容，拒绝覆盖原始记录')
            return {'来源': source, '结果': '已接收过', '原文摘要': digest}
        seen_duplicate = self.connection.execute(
            'SELECT digest FROM duplicates WHERE source=?', (source,)).fetchone()
        if seen_duplicate is not None:
            if seen_duplicate[0] != digest:
                raise ValueError('同一来源出现不同内容，拒绝覆盖原始记录')
            return {'来源': source, '结果': '已接收过', '原文摘要': digest}
        same_revision = self.connection.execute(
            'SELECT id,fingerprint FROM pages WHERE logical_page=? AND ocr_revision=?',
            (logical, revision)).fetchone()
        if same_revision is not None and same_revision[1] != fingerprint:
            raise ValueError('同一逻辑页同一识别版本内容冲突，拒绝覆盖')
        known = self.connection.execute(
            'SELECT id,logical_page,ocr_revision FROM pages WHERE fingerprint=?',
            (fingerprint,)).fetchone()
        if known is not None and (replaces is None or same_revision is not None):
            with self.connection:
                self.connection.execute(
                    'INSERT INTO duplicates VALUES(?,?,?,?,?)',
                    (source, known[0], digest,
                     json.dumps(sorted(set(barcodes)), ensure_ascii=False), seen_at))
            self.raw_store.save(source, body, dict(meta), seen_at)
            return {'来源': source, '结果': '重复扫描', '重复于': f'{known[1]}@{known[2]}',
                    '原文摘要': digest, '词框指纹': fingerprint}
        with self.connection:
            self.connection.execute(
                'INSERT INTO pages(logical_page,ocr_revision,source,batch,rotation,barcodes,'
                'digest,fingerprint,width,height,tokens,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (logical, revision, source, batch, rotation,
                 json.dumps(sorted(set(barcodes)), ensure_ascii=False), digest, fingerprint,
                 width, height, json.dumps(tokens, ensure_ascii=False), seen_at))
        self.raw_store.save(source, body, dict(meta), seen_at)
        return {'来源': source, '结果': '新版本' if replaces else '新页', '逻辑页': logical,
                '识别版本': revision, '原文摘要': digest, '词框指纹': fingerprint}

    # ---------- 页内重建 ----------

    @staticmethod
    def _analyze(page):
        tokens = page['tokens']
        lines = build_lines(tokens)
        header_index = [i for i, line in enumerate(lines) if column_regions(line)]
        header_zone = lines[:header_index[0]] if header_index else lines
        enterprise, enterprise_tokens, kind, evidence_conflict = page_evidence(header_zone)
        rows = []
        for position, index in enumerate(header_index):
            regions = column_regions(lines[index])
            end = header_index[position + 1] if position + 1 < len(header_index) else len(lines)
            for line in lines[index + 1:end]:
                row = row_from_line(line, regions)
                if row is not None:
                    rows.append(row)
        return {'lines': lines, 'has_table_header': bool(header_index), 'rows': rows,
                'enterprise': enterprise, 'enterprise_tokens': enterprise_tokens,
                'kind': kind, 'evidence_conflict': evidence_conflict}

    # ---------- 构建 ----------

    def _active_pages(self):
        rows = self.connection.execute(
            'SELECT id,logical_page,ocr_revision,source,batch,rotation,barcodes,digest,'
            'fingerprint,width,height,tokens,received_at FROM pages').fetchall()
        pages = {}
        for row in rows:
            page = dict(zip(('id', 'logical', 'revision', 'source', 'batch', 'rotation', 'barcodes',
                             'digest', 'fingerprint', 'width', 'height', 'tokens', 'received_at'), row))
            page['barcodes'] = json.loads(page['barcodes'])
            page['tokens'] = json.loads(page['tokens'])
            key = page['logical']
            if key not in pages or int(page['revision']) > int(pages[key]['revision']):
                pages[key] = page
        aliases = {}
        for source, page_id, barcodes in self.connection.execute(
                'SELECT source,page_id,barcodes FROM duplicates ORDER BY source'):
            aliases.setdefault(page_id, []).append({'source': source, 'barcodes': json.loads(barcodes)})
        for page in pages.values():
            page['aliases'] = aliases.get(page['id'], [])
        return [pages[key] for key in sorted(pages)]

    def _decisions(self):
        rows = self.connection.execute(
            'SELECT id,item_key,type,documents,value,bound,reviewer,note,decided_at '
            'FROM decisions ORDER BY id').fetchall()
        decisions = [dict(zip(('id', 'item_key', 'type', 'documents', 'value', 'bound',
                               'reviewer', 'note', 'decided_at'), row)) for row in rows]
        for decision in decisions:
            decision['documents'] = json.loads(decision['documents'])
            decision['bound'] = json.loads(decision['bound'])
        return decisions

    @staticmethod
    def _latest_applicable(latest, item_key, binding):
        """只有最新决定且绑定与当前词框完全一致时才适用；旧决定不套用到新词框。"""
        history = latest.get(item_key, [])
        if history and history[-1]['bound'] == binding:
            return history[-1]
        return None

    def build(self):
        """根据已接收页面与决定日志确定性地重建全部文档状态。"""
        pages = self._active_pages()
        decisions = self._decisions()
        latest = {}
        for decision in decisions:
            latest.setdefault(decision['item_key'], []).append(decision)
        applied = set()
        analyses = {page['logical']: self._analyze(page) for page in pages}

        # 二维码与主体证据的链接，只来自二维码唯一的干净页面
        links = {}
        link_conflicts = set()
        for page in pages:
            analysis = analyses[page['logical']]
            if len(page['barcodes']) == 1 and analysis['enterprise']:
                code = page['barcodes'][0]
                if code in links and links[code] != analysis['enterprise']:
                    link_conflicts.add(code)
                else:
                    links[code] = analysis['enterprise']

        held_pages = []
        attribution = {}
        items = []

        for page in pages:
            logical = page['logical']
            analysis = analyses[logical]
            barcodes = sorted(set(page['barcodes']) |
                              {code for alias in page['aliases'] for code in alias['barcodes']})
            binding = {'page': logical, 'ocr_revision': page['revision'],
                       'fingerprint': page['fingerprint']}
            if len(barcodes) >= 2:
                item = {'key': f'barcode|{logical}|{page["revision"]}', 'type': '二维码冲突',
                        'page': logical, 'ocr_revision': page['revision'], 'documents': barcodes,
                        'candidates': [{'kind': '归属候选', 'value': JOIN_PREFIX + code} for code in barcodes]
                                      + [{'kind': '归属候选', 'value': DISCARD}],
                        'evidence': {'二维码': barcodes, '主体证据': analysis['enterprise'],
                                     '原文摘要': page['digest'], '词框指纹': page['fingerprint']},
                        'binding': binding, 'blocks': barcodes}
                decision = self._latest_applicable(latest, item['key'], binding)
                if decision is not None:
                    applied.add(decision['id'])
                    if decision['value'].startswith(JOIN_PREFIX):
                        attribution[logical] = {'doc': decision['value'][len(JOIN_PREFIX):], 'manual': True}
                    else:
                        held_pages.append(self._held(page, '二维码冲突（已判定不归入任何文档）', item['evidence']))
                else:
                    held_pages.append(self._held(page, '二维码冲突', item['evidence']))
                    items.append(item)
                continue
            if len(barcodes) == 1:
                code = barcodes[0]
                if code in link_conflicts:
                    held_pages.append(self._held(page, '二维码主体证据冲突', {'二维码': code}))
                    continue
                if analysis['enterprise'] and links.get(code) and links[code] != analysis['enterprise']:
                    held_pages.append(self._held(page, '主体证据与二维码冲突',
                                                 {'二维码': code, '主体证据': analysis['enterprise']}))
                    continue
                attribution[logical] = {'doc': code, 'manual': False}
                continue
            candidates = sorted(set(links) | set(link_conflicts))
            item = {'key': f'attrib|{logical}|{page["revision"]}', 'type': '归属证据不足',
                    'page': logical, 'ocr_revision': page['revision'], 'documents': candidates,
                    'candidates': [{'kind': '归属候选', 'value': JOIN_PREFIX + code} for code in candidates]
                                  + [{'kind': '归属候选', 'value': DISCARD}],
                    'evidence': {'主体证据': analysis['enterprise'], '页类': analysis['kind'],
                                 '原文摘要': page['digest'], '词框指纹': page['fingerprint']},
                    'binding': binding, 'blocks': []}
            if analysis['evidence_conflict']:
                held_pages.append(self._held(page, '页内主体证据冲突', item['evidence']))
                continue
            matched = sorted(code for code, enterprise in links.items()
                             if enterprise == analysis['enterprise'] and code not in link_conflicts)
            if analysis['enterprise'] and len(matched) == 1:
                attribution[logical] = {'doc': matched[0], 'manual': False}
                continue
            decision = self._latest_applicable(latest, item['key'], binding)
            if decision is not None:
                applied.add(decision['id'])
                if decision['value'].startswith(JOIN_PREFIX):
                    attribution[logical] = {'doc': decision['value'][len(JOIN_PREFIX):], 'manual': True}
                    continue
                held_pages.append(self._held(page, '归属证据不足（已判定不归入任何文档）', item['evidence']))
            else:
                held_pages.append(self._held(page, '归属证据不足', item['evidence']))
                items.append(item)

        # 文档组装
        known_docs = set(links) | set(link_conflicts)
        for page in pages:
            known_docs.update(page['barcodes'])
        for entry in attribution.values():
            known_docs.add(entry['doc'])
        documents = {}
        for doc in sorted(known_docs):
            member = [page for page in pages
                      if attribution.get(page['logical'], {}).get('doc') == doc]
            documents[doc] = self._assemble_document(doc, member, analyses, links, latest,
                                                     applied, items)

        log = []
        for decision in decisions:
            log.append({'id': decision['id'], '复核项': decision['item_key'], '类型': decision['type'],
                        '值': decision['value'], '复核人': decision['reviewer'],
                        '时间': decision['decided_at'], '备注': decision['note'],
                        '状态': '生效' if decision['id'] in applied else '未生效（词框已变化或已被取代）'})
        return {'文档': documents, '搁置页': held_pages, '复核队列': items, '决定日志': log}

    @staticmethod
    def _held(page, reason, evidence):
        return {'逻辑页': page['logical'], '识别版本': page['revision'], '来源': page['source'],
                '原因': reason, '证据': evidence}

    def _assemble_document(self, doc, member, analyses, links, latest, applied, items):
        reasons = []
        starts = sorted((p for p in member if analyses[p['logical']]['kind'] == 'start'),
                        key=lambda p: p['logical'])
        continuations = sorted((p for p in member if analyses[p['logical']]['kind'] == 'continuation'),
                               key=lambda p: p['logical'])
        others = sorted((p for p in member
                         if analyses[p['logical']]['kind'] not in ('start', 'continuation')),
                        key=lambda p: p['logical'])
        if len(starts) > 1:
            reasons.append('首页证据冲突')
        if len(continuations) > 1:
            reasons.append('续页顺序证据不足')
        ordered = starts + continuations + others
        pages_by = {page['logical']: page for page in ordered}

        stream = []
        for page in ordered:
            for row in analyses[page['logical']]['rows']:
                stream.append((page['logical'], tag_row(page, row)))
        assembled = []
        unclosed = []
        open_row = None
        open_pages = []
        for logical, row in stream:
            complete = bool(row['name_text']) and all(key['text'] for key in row['keys'].values())
            if open_row is not None:
                merged = merge_rows(open_row, row)
                open_pages = open_pages + [logical]
                if merged['name_text'] and all(key['text'] for key in merged['keys'].values()):
                    assembled.append((open_pages, merged))
                    open_row = None
                else:
                    open_row = merged
                continue
            if complete:
                assembled.append(([logical], row))
            elif row['name_text']:
                open_row = row
                open_pages = [logical]
            elif '存在缺货物名称的数量行' not in reasons:
                reasons.append('存在缺货物名称的数量行')
        if open_row is not None:
            unclosed.append((open_pages, open_row))
            reasons.append('存在未闭合货物行')

        # 行级复核项与字段组装
        name_count = {}
        out_rows = []
        for pages_of_row, row in assembled:
            name_count[row['name_text']] = name_count.get(row['name_text'], 0) + 1
            row_key = f"{row['name_text']}#{name_count[row['name_text']]}"
            conflicts = {column: (former, latter)
                         for column, former, latter in row.get('key_conflicts', [])}
            keys_out = {}
            for column, key in row['keys'].items():
                keys_out[column] = self._key_field(
                    doc, row_key, column, key, conflicts.get(column), pages_by,
                    latest, applied, items)
            out_rows.append({'row_key': row_key, 'pages': pages_of_row, 'row': row,
                             'keys': keys_out, 'cross_page': len(set(pages_of_row)) > 1})

        enterprise = links.get(doc)
        enterprise_refs = []
        for page in ordered:
            analysis = analyses[page['logical']]
            if analysis['enterprise'] and analysis['enterprise_tokens'] and (
                    enterprise is None or analysis['enterprise'] == enterprise):
                enterprise = enterprise or analysis['enterprise']
                enterprise_refs = self._refs(page, analysis['enterprise_tokens'])
                break
        if not member:
            reasons.append('无归属页面')
        if member and not out_rows:
            reasons.append('无可用货物行')
        if enterprise is None and member:
            reasons.append('缺主体证据')
        blocking = [item['key'] for item in items if doc in item.get('blocks', [])]
        status = '可发布' if member and out_rows and not reasons and not blocking else '暂停'
        return {'主体': enterprise, '主体证据': enterprise_refs, 'ordered_pages': ordered,
                'pages_by': pages_by,
                'kinds': {p['logical']: analyses[p['logical']]['kind'] for p in ordered},
                'rows': out_rows, 'unclosed': unclosed, '状态': status,
                '原因': reasons + [f'待决复核项 {key}' for key in blocking]}

    def _key_field(self, doc, row_key, column, key, conflict, pages_by, latest, applied, items):
        if conflict is not None:
            former, latter = conflict
            binding = {'tokens': self._binding(former['tokens'] + latter['tokens'], pages_by)}
            item_key = f'keyconf|{doc}|{row_key}|{column}'
            item = {'key': item_key, 'type': '候选冲突', 'document': doc,
                    'field': f'rows[{row_key}].{column}',
                    'candidates': [
                        {'kind': '前半段', 'value': former['text'],
                         'tokens': self._binding(former['tokens'], pages_by)},
                        {'kind': '后半段', 'value': latter['text'],
                         'tokens': self._binding(latter['tokens'], pages_by)}],
                    'evidence': {'相关词框': binding['tokens']},
                    'binding': binding, 'blocks': [doc]}
            decision = self._latest_applicable(latest, item_key, binding)
            if decision is not None:
                applied.add(decision['id'])
                return {'text': decision['value'], 'source': '人工决定', 'decision': decision['id']}
            items.append(item)
            return {'text': None, 'source': '候选冲突', 'decision': None}

        tokens = key['tokens']
        binding = {'tokens': self._binding(tokens, pages_by)}
        low = [t for t in tokens if t['confidence'] is None or t['confidence'] < LOW_CONFIDENCE]
        parsable = bool(NUMBER.match(key['text'])) if key['text'] else False
        if not low and parsable:
            return {'text': key['text'], 'source': 'OCR', 'decision': None}
        confidences = [t['confidence'] for t in tokens if t['confidence'] is not None]
        candidates = [{'kind': 'OCR原文', 'value': key['text'],
                       'confidence': min(confidences) if confidences else None,
                       'tokens': binding['tokens']}]
        guess = key['text'].translate(CONFUSABLE)
        if guess != key['text'] and NUMBER.match(guess):
            candidates.append({'kind': '形近字猜测', 'value': guess})
        item_key = f'lowconf|{doc}|{row_key}|{column}'
        item = {'key': item_key, 'type': '低置信关键值', 'document': doc,
                'field': f'rows[{row_key}].{column}', 'candidates': candidates,
                'evidence': {'相关词框': binding['tokens'], '低置信': [t['id'] for t in low]},
                'binding': binding, 'blocks': [doc]}
        decision = self._latest_applicable(latest, item_key, binding)
        if decision is not None:
            applied.add(decision['id'])
            return {'text': decision['value'], 'source': '人工决定', 'decision': decision['id']}
        items.append(item)
        return {'text': key['text'], 'source': 'OCR(待复核)', 'decision': None}

    @staticmethod
    def _binding(tokens, pages_by):
        return [{'page': token['page'], 'ocr_revision': pages_by[token['page']]['revision'],
                 'token': token['id'], 'text': token['text']} for token in tokens]

    @staticmethod
    def _refs(page, tokens):
        return [{'页': page['logical'], '识别版本': page['revision'], '词框': token['id'],
                 '原文': token['text'], '框': token['box'], '置信': token['confidence'],
                 '原文摘要': page['digest']} for token in tokens]

    # ---------- 复核决定 ----------

    def review_queue(self):
        """复核人看到的待决项：证据与候选，不是无来源的拼接字符串。"""
        return self.build()['复核队列']

    def decide(self, item_key, value, reviewer, note, decided_at):
        """记录人工决定，绑定当前词框证据（页、识别版本、词框 ID、原文）。"""
        if not reviewer or not decided_at:
            raise ValueError('复核人与决定时间不可缺少')
        snapshot = self.build()
        item = next((entry for entry in snapshot['复核队列'] if entry['key'] == item_key), None)
        if item is None:
            raise KeyError(f'无待决复核项: {item_key}')
        if item['type'] in ('二维码冲突', '归属证据不足', '候选冲突'):
            allowed = [candidate['value'] for candidate in item['candidates']]
            if value not in allowed:
                raise ValueError(f'决定必须是候选之一: {allowed}')
        elif not value:
            raise ValueError('决定值不能为空')
        documents = ([item['document']] if item.get('document')
                     else list(item.get('documents', [])))
        with self.connection:
            cursor = self.connection.execute(
                'INSERT INTO decisions(item_key,type,documents,value,bound,reviewer,note,decided_at) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (item_key, item['type'], json.dumps(documents, ensure_ascii=False), value,
                 json.dumps(item['binding'], ensure_ascii=False, sort_keys=True), reviewer, note,
                 decided_at))
        return {'id': cursor.lastrowid, '复核项': item_key, '值': value, '绑定': item['binding']}

    # ---------- 输出 ----------

    def document(self, doc):
        """单个文档的结构化结果，每个字段指回原页词框。"""
        snapshot = self.build()
        if doc not in snapshot['文档']:
            raise KeyError(doc)
        return self._document_out(doc, snapshot['文档'][doc])

    def _document_out(self, doc, entry):
        pages_by = entry['pages_by']
        rows = []
        for index, out in enumerate(entry['rows'], 1):
            row = out['row']
            keys = {}
            for column, field in out['keys'].items():
                keys[column] = {
                    '值': field['text'],
                    '数值': (float(field['text'])
                             if field['text'] and NUMBER.match(field['text']) else None),
                    '来源': field['source'] if field['decision'] is None
                            else {'人工决定': field['decision']},
                    '证据': self._tagged_refs(pages_by, row['keys'][column]['tokens'])}
            rows.append({'序号': index,
                         '名称': {'值': row['name_text'],
                                  '证据': self._tagged_refs(pages_by, row['name_tokens'])},
                         '关键值': keys, '跨页': out['cross_page'], '来源页': out['pages']})
        unclosed = [{'名称': row['name_text'], '来源页': pages,
                     '证据': self._tagged_refs(pages_by, row['name_tokens'])}
                    for pages, row in entry['unclosed']]
        return {'文档': doc, '主体': entry['主体'], '主体证据': entry['主体证据'],
                '页面': [{'逻辑页': page['logical'], '识别版本': page['revision'],
                          '原文摘要': page['digest'], '页类': entry['kinds'][page['logical']],
                          '重复扫描别名': [alias['source'] for alias in page['aliases']]}
                         for page in entry['ordered_pages']],
                '货物行': rows, '未闭合行': unclosed,
                '状态': entry['状态'], '原因': entry['原因']}

    @staticmethod
    def _tagged_refs(pages_by, tokens):
        refs = []
        for token in tokens:
            page = pages_by[token['page']]
            refs.append({'页': token['page'], '识别版本': page['revision'], '词框': token['id'],
                         '原文': token['text'], '框': token['box'], '置信': token['confidence'],
                         '原文摘要': page['digest']})
        return refs

    def all_revisions(self):
        """全部逻辑页的全部识别版本；被替换的旧版本原文仍可读取。"""
        rows = self.connection.execute(
            'SELECT logical_page,ocr_revision,digest FROM pages '
            'ORDER BY logical_page,CAST(ocr_revision AS INTEGER)').fetchall()
        return [{'逻辑页': logical, '识别版本': revision, '原文摘要': digest}
                for logical, revision, digest in rows]

    def publication(self):
        """哪些文档可发布、哪些还欠证据。"""
        snapshot = self.build()
        publishable = sorted(doc for doc, entry in snapshot['文档'].items()
                             if entry['状态'] == '可发布')
        paused = {doc: entry['原因'] for doc, entry in snapshot['文档'].items()
                  if entry['状态'] != '可发布'}
        return {'可发布': publishable, '暂停': paused, '搁置页': snapshot['搁置页']}

    def close(self):
        self.connection.close()
