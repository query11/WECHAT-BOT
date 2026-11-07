"""Bounded, deterministic checks over declared artifact bytes; no file access or AI.

The caller selects an exact declared artifact basename and supplies its bytes.
This module never resolves paths, follows links, executes formulas, or repairs data.
"""
from collections import Counter
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import io
import posixpath
import re
import stat
import xml.etree.ElementTree as ET
import zipfile


MAX_CONTENT_BYTES = 10 * 1024 * 1024
MAX_CHECK_TEXT = 4000
MAX_RULES = 12
MAX_NAMES = 20
MAX_ROWS = 20000
MAX_COLUMNS = 200
MAX_ZIP_MEMBERS = 1024
MAX_ZIP_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 32 * 1024 * 1024
_SHEET_NS = {'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
             'http://purl.oclc.org/ooxml/spreadsheetml/main'}
_WORD_NS = {'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
            'http://purl.oclc.org/ooxml/wordprocessingml/main'}
_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_DOC_REL_NS = {'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
               'http://purl.oclc.org/ooxml/officeDocument/relationships'}
_NUMBER = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')
_KINDS = {'必需列': 'required_columns', '列合计': 'column_sum',
          '必需章节': 'required_sections'}


class CheckDefinitionError(ValueError):
    """A user-authored check definition is invalid."""


class _Unverified(ValueError):
    pass


class _Failed(ValueError):
    pass


def _clean_field(value):
    return (isinstance(value, str) and bool(value.strip())
            and not re.search(r'[\x00-\x1f\x7f|｜]', value))


def _basename(value):
    if (not _clean_field(value) or len(value) > 255 or value in {'.', '..'}
            or re.search(r'[/\\:*?"<>]', value) or value.endswith(('.', ' '))):
        raise CheckDefinitionError('检查文件必须是明确的文件名，不能包含路径或通配符。')
    return value


def _names(value):
    names = [item.strip() for item in re.split('[,，]', value)]
    if not 1 <= len(names) <= MAX_NAMES or any(not _clean_field(item) for item in names):
        raise CheckDefinitionError('每条检查需指定 1 至 20 个非空列名或章节名。')
    if len(set(names)) != len(names):
        raise CheckDefinitionError('同一条检查中的列名或章节名不能重复。')
    return names


def _decimal(value):
    if not isinstance(value, str):
        raise _Unverified('金额不是可确认的数字。')
    value = value.strip()
    if not value or len(value) > 128 or not _NUMBER.fullmatch(value):
        raise _Unverified('金额列包含空白或非数字，未确认合计。')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise _Unverified('金额不是可确认的数字。') from None
    if not result.is_finite() or abs(result.as_tuple().exponent) > 1000:
        raise _Unverified('金额精度或数量级超出检查范围。')
    return result


def parse_checks(text):
    """Parse optional newline-separated checks into JSON-serializable rule dicts."""
    if not isinstance(text, str):
        raise CheckDefinitionError('结果检查必须是文本。')
    if len(text) > MAX_CHECK_TEXT:
        raise CheckDefinitionError('结果检查不能超过 4000 字符。')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > MAX_RULES:
        raise CheckDefinitionError('最多设置 12 条结果检查。')
    rules = []
    for line in lines:
        parts = [part.strip() for part in re.split('[|｜]', line)]
        kind = _KINDS.get(parts[0])
        length = {'required_columns': 4, 'column_sum': 5, 'required_sections': 3}.get(kind)
        if length is None or len(parts) != length or any(not _clean_field(p) for p in parts):
            raise CheckDefinitionError('结果检查格式不正确，请按必需列、列合计或必需章节示例填写。')
        rule = {'kind': kind, 'file': _basename(parts[1])}
        extension = posixpath.splitext(rule['file'])[1].lower()
        supported = {'.md', '.txt', '.docx'} if kind == 'required_sections' else {'.csv', '.xlsx'}
        if extension not in supported:
            raise CheckDefinitionError('列检查仅支持 CSV/XLSX；章节检查仅支持 MD/TXT/DOCX。')
        if kind == 'required_sections':
            rule['sections'] = _names(parts[2])
        else:
            if len(parts[2]) > 128:
                raise CheckDefinitionError('工作表名称过长。')
            rule['sheet'] = parts[2]
            if rule['file'].lower().endswith('.csv') and rule['sheet'] != '-':
                raise CheckDefinitionError('CSV 的工作表栏请填写 -。')
            if kind == 'required_columns':
                rule['columns'] = _names(parts[3])
            else:
                rule['column'] = parts[3]
                try:
                    rule['expected'] = str(_decimal(parts[4]))
                except _Unverified:
                    raise CheckDefinitionError('列合计的目标值必须是有限数字，不能包含公式或单位。') from None
        rules.append(rule)
    return rules


def _validated_rule(rule):
    if not isinstance(rule, dict):
        raise _Unverified('检查条件无效。')
    kind = rule.get('kind')
    label = next((label for label, value in _KINDS.items() if value == kind), None)
    fields = [label, rule.get('file')]
    if kind in {'required_columns', 'column_sum'}:
        fields.append(rule.get('sheet'))
    if kind in {'required_columns', 'required_sections'}:
        values = rule.get('columns' if kind == 'required_columns' else 'sections')
        if not isinstance(values, list) or any(not _clean_field(v) or ',' in v or '，' in v for v in values):
            raise _Unverified('检查条件无效。')
        fields.append(','.join(values))
    elif kind == 'column_sum':
        fields.extend((rule.get('column'), rule.get('expected')))
    if any(not _clean_field(field) for field in fields):
        raise _Unverified('检查条件无效。')
    try:
        return parse_checks(' | '.join(fields))[0]
    except (CheckDefinitionError, IndexError):
        raise _Unverified('检查条件无效。') from None


def _utf8(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise _Unverified('文本不是有效的 UTF-8 编码，未执行内容检查。') from None
    if '\x00' in text:
        raise _Unverified('文本包含无法确认的控制字符。')
    return text


def _xml(raw):
    # Removing NULs also catches declarations in UTF-16/UTF-32 XML before parsing.
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', raw.replace(b'\x00', b''), re.I):
        raise _Unverified('文件包含不支持的 XML 声明。')
    try:
        return ET.fromstring(raw)
    except (ET.ParseError, ValueError, LookupError):
        raise _Unverified('文件中的 XML 无法完整解析。') from None


def _namespace(root, allowed, local_name):
    for ns in allowed:
        if root.tag == '{' + ns + '}' + local_name:
            return ns
    raise _Unverified('文件结构或命名空间不受支持。')


class _Package:
    def __init__(self, raw):
        self.archive = zipfile.ZipFile(io.BytesIO(raw))
        self.members = {}
        infos = self.archive.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise _Unverified('文件内部成员数量超出检查范围。')
        total = 0
        for info in infos:
            name = info.filename
            if (not name or info.orig_filename != name or name.startswith('/') or '\\' in name or ':' in name
                    or '..' in name.split('/') or name in self.members
                    or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)):
                raise _Unverified('文件内部成员存在歧义或不支持的结构。')
            if info.file_size > MAX_ZIP_MEMBER_BYTES:
                raise _Unverified('文件内部成员大小超出检查范围。')
            total += info.file_size
            if total > MAX_ZIP_EXPANDED_BYTES:
                raise _Unverified('文件展开大小超出检查范围。')
            self.members[name] = info

    def read(self, name):
        info = self.members.get(name)
        if info is None or info.is_dir():
            raise _Unverified('文件缺少所需的内部成员。')
        with self.archive.open(info) as stream:
            data = stream.read(MAX_ZIP_MEMBER_BYTES + 1)
        if len(data) != info.file_size or len(data) > MAX_ZIP_MEMBER_BYTES:
            raise _Unverified('文件内部成员大小无法确认。')
        return data

    def xml(self, name):
        return _xml(self.read(name))


@dataclass
class _Cell:
    text: str = ''
    formula: bool = False
    numeric: bool = True


def _blank_row(row):
    return all(not c.text.strip() and not c.formula for c in row)


def _csv_rows(raw):
    rows = []
    try:
        for index, row in enumerate(csv.reader(io.StringIO(_utf8(raw), newline=''), strict=True), 1):
            if index > MAX_ROWS or len(row) > MAX_COLUMNS:
                raise _Unverified('表格行数或列数超出检查范围。')
            cells = [_Cell(value) for value in row]
            if not _blank_row(cells):
                rows.append(cells)
    except csv.Error:
        raise _Unverified('CSV 格式无法完整解析。') from None
    return rows


def _xlsx_rows(raw, sheet_name):
    package = _Package(raw)
    workbook = package.xml('xl/workbook.xml')
    ns = _namespace(workbook, _SHEET_NS, 'workbook')
    sheet_lists = workbook.findall('{' + ns + '}sheets')
    if len(sheet_lists) != 1:
        raise _Unverified('工作簿缺少工作表清单。')
    sheets = sheet_lists[0]
    matches = [s for s in sheets if s.tag == '{' + ns + '}sheet' and s.get('name') == sheet_name]
    if not matches:
        raise _Failed('未找到指定工作表。')
    if len(matches) != 1:
        raise _Unverified('指定工作表名称重复，无法唯一确认。')
    ids = [matches[0].get('{' + rns + '}id') for rns in _DOC_REL_NS]
    ids = [value for value in ids if value]
    if len(ids) != 1:
        raise _Unverified('工作表关联无法唯一确认。')
    rels = package.xml('xl/_rels/workbook.xml.rels')
    _namespace(rels, {_REL_NS}, 'Relationships')
    relation_map = {}
    for rel in rels:
        if rel.tag != '{' + _REL_NS + '}Relationship':
            continue
        key = rel.get('Id')
        if not key or key in relation_map:
            raise _Unverified('工作表关联存在重复或缺失。')
        relation_map[key] = rel
    relation = relation_map.get(ids[0])
    if relation is None:
        raise _Unverified('工作表关联缺失。')
    if relation.get('TargetMode', 'Internal') != 'Internal' or relation.get('Type') not in {
            rns + '/worksheet' for rns in _DOC_REL_NS}:
        raise _Unverified('工作表关联类型不受支持。')
    target = relation.get('Target', '')
    if not target or '\\' in target or ':' in target or '..' in target.split('/'):
        raise _Unverified('工作表关联路径不受支持。')
    target = target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target)
    if posixpath.normpath(target) != target:
        raise _Unverified('工作表关联路径无法唯一确认。')
    shared = []
    if 'xl/sharedStrings.xml' in package.members:
        strings = package.xml('xl/sharedStrings.xml')
        _namespace(strings, {ns}, 'sst')
        for item in strings.findall('{' + ns + '}si'):
            # Exclude phonetic text (rPh); it is not the cell's displayed content.
            shared.append(''.join(node.text or '' for child in item
                                  if child.tag in {'{' + ns + '}t', '{' + ns + '}r'}
                                  for node in ([child] if child.tag.endswith('}t')
                                               else child.findall('{' + ns + '}t'))))
    worksheet = package.xml(target)
    _namespace(worksheet, {ns}, 'worksheet')
    sheet_data = worksheet.findall('{' + ns + '}sheetData')
    if len(sheet_data) != 1:
        raise _Unverified('工作表数据区域无法唯一确认。')
    rows = []
    header_row = None
    formula_ranges = []
    previous_row = 0
    for row_node in sheet_data[0]:
        if row_node.tag != '{' + ns + '}row':
            continue
        row_number = _positive_int(row_node.get('r', str(previous_row + 1)))
        if row_number <= previous_row or row_number > MAX_ROWS:
            raise _Unverified('工作表行位置重复或超出检查范围。')
        previous_row = row_number
        cells = []
        previous_column = 0
        for cell in row_node:
            if cell.tag != '{' + ns + '}c':
                continue
            reference = cell.get('r')
            if reference:
                match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]*)', reference)
                if not match or _positive_int(match[2]) != row_number:
                    raise _Unverified('工作表单元格位置无法确认。')
                column = 0
                for char in match[1]:
                    column = column * 26 + ord(char) - ord('A') + 1
            else:
                column = previous_column + 1
            if column <= previous_column or column > MAX_COLUMNS:
                raise _Unverified('工作表列位置重复或超出检查范围。')
            previous_column = column
            while len(cells) < column - 1:
                cells.append(_Cell())
            formulas = cell.findall('{' + ns + '}f')
            values = cell.findall('{' + ns + '}v')
            inline = cell.findall('{' + ns + '}is')
            if len(values) > 1 or len(inline) > 1 or len(formulas) > 1:
                raise _Unverified('单元格包含重复内容，无法确认。')
            if formulas and formulas[0].get('ref'):
                formula_ranges.append(_formula_range(formulas[0].get('ref')))
            value = values[0].text or '' if values else ''
            kind = cell.get('t', 'n')
            if kind == 's':
                if len(values) != 1 or inline:
                    raise _Unverified('工作表共享文本格式无效。')
                index = _nonnegative_int(value)
                if index >= len(shared):
                    raise _Unverified('工作表共享文本索引无效。')
                value = shared[index]
            elif kind == 'inlineStr':
                if len(inline) != 1 or values:
                    raise _Unverified('工作表内联文本格式无效。')
                value = ''.join(node.text or '' for child in inline[0]
                                if child.tag in {'{' + ns + '}t', '{' + ns + '}r'}
                                for node in ([child] if child.tag.endswith('}t')
                                             else child.findall('{' + ns + '}t')))
            elif kind not in {'n', 'str', 'b', 'e', 'd'}:
                raise _Unverified('工作表包含不支持的单元格类型。')
            elif inline:
                raise _Unverified('单元格类型与内容不一致。')
            cells.append(_Cell(value, bool(formulas), kind not in {'b', 'e', 'd'}))
        if not _blank_row(cells):
            rows.append(cells)
            if header_row is None:
                header_row = row_number
    return rows, formula_ranges, header_row


def _formula_range(reference):
    match = re.fullmatch(r'\$?([A-Z]{1,3})\$?([1-9][0-9]*)(?::\$?([A-Z]{1,3})\$?([1-9][0-9]*))?', reference)
    if not match:
        raise _Unverified('公式作用区域无法确认。')
    def column(letters):
        result = 0
        for letter in letters:
            result = result * 26 + ord(letter) - ord('A') + 1
        return result
    first_col, last_col = column(match[1]), column(match[3] or match[1])
    first_row, last_row = _positive_int(match[2]), _positive_int(match[4] or match[2])
    if not (first_col <= last_col <= MAX_COLUMNS and first_row <= last_row <= MAX_ROWS):
        raise _Unverified('公式作用区域超出检查范围。')
    return first_col, first_row, last_col, last_row


def _nonnegative_int(value):
    if not re.fullmatch(r'[0-9]{1,9}', value):
        raise _Unverified('文件中的位置或索引格式无效。')
    return int(value)


def _positive_int(value):
    result = _nonnegative_int(value)
    if result < 1:
        raise _Unverified('文件中的位置或索引格式无效。')
    return result


def _table_check(rule, raw):
    extension = posixpath.splitext(rule['file'])[1].lower()
    formula_ranges = []
    header_row = None
    if extension == '.csv':
        rows = _csv_rows(raw)
    elif extension == '.xlsx':
        rows, formula_ranges, header_row = _xlsx_rows(raw, rule['sheet'])
    else:
        raise _Unverified('列检查目前仅支持 CSV 和 XLSX。')
    if not rows:
        raise _Failed('表格为空，未找到必需列。')
    header = rows[0]
    if any(first_row <= header_row <= last_row for _, first_row, _, last_row in formula_ranges):
        raise _Unverified('列名位于公式作用区域，无法确认实际列名。')
    if any(c.formula for c in header):
        raise _Unverified('列名包含公式，无法确认实际列名。')
    if any(not c.numeric for c in header):
        raise _Unverified('列名包含错误值或不支持的类型。')
    names = [c.text.strip() for c in header]
    nonempty = [name for name in names if name]
    if len(nonempty) != len(set(nonempty)):
        raise _Unverified('表格存在重复列名，无法唯一确认。')
    required = rule['columns'] if rule['kind'] == 'required_columns' else [rule['column']]
    missing = [name for name in required if name not in names]
    if missing:
        raise _Failed(_missing_detail(missing, '必需列'))
    if rule['kind'] == 'required_columns':
        return '已确认 {} 个必需列。'.format(len(required))
    index = names.index(rule['column'])
    # Array/shared formulas may cache results into cells with no local <f>.
    if any(first_col <= index + 1 <= last_col for first_col, _, last_col, _ in formula_ranges):
        raise _Unverified('金额列位于公式作用区域；未使用公式缓存值判定。')
    if len(rows) < 2:
        raise _Unverified('金额列没有数据行，未确认合计。')
    with localcontext() as context:
        context.prec = 4096
        total = Decimal(0)
        for row in rows[1:]:
            cell = row[index] if index < len(row) else _Cell()
            if cell.formula:
                raise _Unverified('金额列包含公式；未使用公式缓存值判定。')
            if not cell.numeric:
                raise _Unverified('金额列包含错误值或非数字类型。')
            total += _decimal(cell.text)
        if total != _decimal(rule['expected']):
            raise _Failed('金额列合计与目标值不一致。')
    return '金额列合计与目标值一致（{} 行）。'.format(len(rows) - 1)


def _normalize_markdown_heading(value):
    value = value.strip()
    value = re.sub(r'(?<!\\)(\*\*|__|~~)(.+?)\1', r'\2', value)
    value = re.sub(r'(?<!\\)(?<!\S)([*_])(\S(?:.*?\S)?)\1(?=\s|$)', r'\2', value)
    return value.strip()


def _markdown_headings(text):
    headings = []
    fence = None
    paragraph = []
    html_block = None
    for line in text.splitlines():
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}[ \t]*', line):
                fence = None
            continue
        if html_block:
            if ((html_block == 'blank' and not line.strip())
                    or (html_block != 'blank' and html_block in line.lower())):
                html_block = None
            continue
        stripped = line.lstrip(' ')
        if len(line) - len(stripped) <= 3:
            special_html = next((end for start, end in (('<?', '?>'), ('<![CDATA[', ']]>'))
                                 if stripped.startswith(start)), None)
            if special_html:
                if special_html not in stripped:
                    html_block = special_html
                paragraph = []
                continue
            if re.match(r'<![A-Z]', stripped):
                if '>' not in stripped:
                    html_block = '>'
                paragraph = []
                continue
            if stripped.startswith('<!--'):
                if '-->' not in stripped[4:]:
                    html_block = '-->'
                paragraph = []
                continue
            raw_html = re.match(r'<(script|pre|style|textarea)(?:[\s>]|$)', stripped, re.I)
            if raw_html:
                terminator = '</' + raw_html[1].lower() + '>'
                if terminator not in stripped.lower():
                    html_block = terminator
                paragraph = []
                continue
            if re.match(r'</?(?:address|article|aside|blockquote|body|details|div|dl|fieldset|figure|footer|form|h[1-6]|head|header|html|iframe|li|main|nav|ol|p|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)(?:[\s/>]|$)', stripped, re.I):
                html_block = 'blank'
                paragraph = []
                continue
            if not paragraph and re.fullmatch(r'</?[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[^<>]*)?/?>[ \t]*', stripped):
                html_block = 'blank'
                continue
        opening = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if not opening:
            # A fenced block may begin as the first content of a list item.
            # The marker itself is a block container, so find its fence before
            # treating indented lines inside it as ordinary document headings.
            opening = re.match(r'^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+(`{3,}|~{3,})(.*)$', line)
        if opening and (opening[1][0] != '`' or '`' not in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            paragraph = []
            continue
        atx = re.match(r'^ {0,3}#{1,6}(?:[ \t]+(.*)|$)', line)
        if atx:
            heading = _normalize_markdown_heading(re.sub(r'[ \t]+#+[ \t]*$', '', atx[1] or ''))
            # Inline emphasis and code markers are presentation syntax, not
            # part of the visible section name used by the template rule.
            heading = re.sub(r'(?<!\\)(\*\*|__|~~|`)(.*?)\1', r'\2', heading)
            heading = re.sub(r'(?<!\\)([*_`~])([^\s].*?[^\s])\1', r'\2', heading).strip()
            if heading:
                headings.append(heading)
            paragraph = []
        elif re.fullmatch(r' {0,3}(?:=+|-+)[ \t]*', line) and paragraph:
            headings.append(_normalize_markdown_heading(' '.join(paragraph)))
            paragraph = []
        elif not line.strip() or line.startswith(('    ', '\t')) or re.match(r'^ {0,3}(?:>|[-+*] |\d+[.)] )', line):
            paragraph = []
        else:
            paragraph.append(line.strip())
    return headings


def _heading_style(value):
    return bool(re.fullmatch(r'(?:heading|标题)\s*[1-9]', value.strip(), re.I))


def _paragraph_text(paragraph, ns):
    pieces = []
    def visit(node):
        # A nested text-box paragraph is not part of the enclosing heading.
        if node is not paragraph and node.tag == '{' + ns + '}p':
            return
        if node.tag == '{' + ns + '}t':
            pieces.append(node.text or '')
        elif node.tag == '{' + ns + '}tab':
            pieces.append('\t')
        elif node.tag in {'{' + ns + '}br', '{' + ns + '}cr'}:
            pieces.append('\n')
        else:
            for child in node:
                visit(child)
    visit(paragraph)
    return ''.join(pieces).strip()


def _docx_headings(raw):
    package = _Package(raw)
    document = package.xml('word/document.xml')
    ns = _namespace(document, _WORD_NS, 'document')
    bodies = document.findall('{' + ns + '}body')
    if len(bodies) != 1:
        raise _Unverified('文档正文区域无法唯一确认。')
    style_ids = set()
    if 'word/styles.xml' in package.members:
        styles = package.xml('word/styles.xml')
        _namespace(styles, {ns}, 'styles')
        seen = set()
        for style in styles.findall('{' + ns + '}style'):
            identifier = style.get('{' + ns + '}styleId', '')
            if not identifier or identifier in seen:
                raise _Unverified('文档段落样式存在重复或缺失。')
            seen.add(identifier)
            name = style.find('{' + ns + '}name')
            if (style.get('{' + ns + '}type') == 'paragraph'
                    and name is not None and _heading_style(name.get('{' + ns + '}val', ''))):
                style_ids.add(identifier)
    headings = []
    for index, paragraph in enumerate(bodies[0].iter('{' + ns + '}p'), 1):
        if index > MAX_ROWS:
            raise _Unverified('文档段落数量超出检查范围。')
        properties = paragraph.findall('{' + ns + '}pPr')
        if len(properties) > 1:
            raise _Unverified('文档段落样式无法唯一确认。')
        if not properties:
            continue
        styles = properties[0].findall('{' + ns + '}pStyle')
        if len(styles) > 1:
            raise _Unverified('文档段落样式无法唯一确认。')
        if not styles:
            continue
        identifier = styles[0].get('{' + ns + '}val', '')
        if _heading_style(identifier) or identifier in style_ids:
            headings.append(_paragraph_text(paragraph, ns))
    return headings


def _missing_detail(names, label):
    # Only echo user-authored rule names, never text discovered in the artifact.
    shown = []
    for name in names:
        shortened = name if len(name) <= 40 else name[:39] + '…'
        candidate = '、'.join(shown + [shortened])
        if len(candidate) > 220:
            break
        shown.append(shortened)
    suffix = '（另有 {} 项）'.format(len(names) - len(shown)) if len(shown) < len(names) else ''
    return '缺少{}：{}{}。'.format(label, '、'.join(shown), suffix)


def _section_check(rule, raw):
    extension = posixpath.splitext(rule['file'])[1].lower()
    if extension == '.docx':
        headings = _docx_headings(raw)
    elif extension == '.md':
        headings = _markdown_headings(_utf8(raw))
    elif extension == '.txt':
        headings = [line.strip() for line in _utf8(raw).splitlines() if line.strip()]
    else:
        raise _Unverified('章节检查目前仅支持 Markdown、TXT 和 DOCX。')
    counts = Counter(headings)
    if any(counts[name] > 1 for name in rule['sections']):
        raise _Unverified('必需章节标题重复，无法唯一确认。')
    missing = [name for name in rule['sections'] if counts[name] == 0]
    if missing:
        raise _Failed(_missing_detail(missing, '必需章节标题'))
    return '已确认 {} 个必需章节标题。'.format(len(rule['sections']))


def evaluate_rule(rule, raw):
    """Return passed/failed/unverified, never including supplied document contents."""
    try:
        rule = _validated_rule(rule)
        if not isinstance(raw, bytes):
            raise _Unverified('未取得可读取的文件内容。')
        if len(raw) > MAX_CONTENT_BYTES:
            raise _Unverified('文件超过 10 MiB，未执行内容检查。')
        detail = (_section_check(rule, raw) if rule['kind'] == 'required_sections'
                  else _table_check(rule, raw))
        return {'status': 'passed', 'detail': detail}
    except _Failed as exc:
        return {'status': 'failed', 'detail': str(exc)}
    except _Unverified as exc:
        return {'status': 'unverified', 'detail': str(exc)}
    except Exception:
        # Parser/library errors must never promote an artifact to a passing result.
        return {'status': 'unverified', 'detail': '文件格式无法安全完整解析，未确认检查结果。'}
