from xml.etree import ElementTree

NAMESPACE='http://www.loc.gov/standards/alto/ns-v4#'


def words(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('不接受外部实体或 DTD')
    root=ElementTree.fromstring(raw)
    pages=[]
    for page in root.findall(f'.//{{{NAMESPACE}}}Page'):
        width=int(page.attrib['WIDTH'])
        height=int(page.attrib['HEIGHT'])
        if min(width,height)<=0:
            raise ValueError('页面尺寸必须为正数')
        tokens=[]
        identifiers=set()
        for token in page.findall(f'.//{{{NAMESPACE}}}String'):
            identity=token.attrib['ID']
            if identity in identifiers:
                raise ValueError('页内词框 ID 重复')
            identifiers.add(identity)
            left,top,box_width,box_height=(int(token.attrib[key]) for key in ('HPOS','VPOS','WIDTH','HEIGHT'))
            if min(left,top,box_width,box_height)<0 or left+box_width>width or top+box_height>height:
                raise ValueError('词框超出页面')
            confidence=float(token.attrib['WC']) if 'WC' in token.attrib else None
            if confidence is not None and not 0<=confidence<=1:
                raise ValueError('置信度超出范围')
            tokens.append({'id':identity,'text':token.attrib['CONTENT'],'box':[left,top,box_width,box_height],'confidence':confidence})
        pages.append({'page_id':page.attrib['ID'],'width':width,'height':height,'tokens':tokens})
    return pages
