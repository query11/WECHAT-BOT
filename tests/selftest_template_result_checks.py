"""Deterministic template checks: in-memory fixtures, no model or production data."""
import io
import unittest
from unittest.mock import patch
import warnings
from xml.sax.saxutils import escape
import zipfile

import template_result_checks as C


S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def package(members):
    result = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members:
                archive.writestr(name, content)
    return result.getvalue()


def cell(ref, value, kind='inlineStr', formula=''):
    content = ('<is><t>' + escape(value) + '</t></is>' if kind == 'inlineStr'
               else '<v>' + escape(value) + '</v>')
    return '<c r="' + ref + '" t="' + kind + '">' + formula + content + '</c>'


def row(number, *cells):
    return '<row r="' + str(number) + '">' + ''.join(cells) + '</row>'


def xlsx(rows=None, sheets=None, rel=None, extras=(), sheet_xml=None, workbook_xml=None):
    if rows is None:
        rows = row(1, cell('A1', '项目'), cell('B1', '金额')) + row(2, cell('A2', 'a'), cell('B2', '0.1', 'n')) + row(3, cell('A3', 'b'), cell('B3', '0.2', 'n'))
    sheets = '<sheet name="汇总" sheetId="1" r:id="rId1"/>' if sheets is None else sheets
    rel = '<Relationship Id="rId1" Type="' + R + '/worksheet" Target="worksheets/sheet1.xml"/>' if rel is None else rel
    members = [
        ('xl/workbook.xml', workbook_xml or '<workbook xmlns="' + S + '" xmlns:r="' + R + '"><sheets>' + sheets + '</sheets></workbook>'),
        ('xl/_rels/workbook.xml.rels', '<Relationships xmlns="' + P + '">' + rel + '</Relationships>'),
        ('xl/worksheets/sheet1.xml', sheet_xml or '<worksheet xmlns="' + S + '"><sheetData>' + rows + '</sheetData></worksheet>'),
    ]
    return package(members + list(extras))


def paragraph(text, style=None):
    properties = '<w:pPr><w:pStyle w:val="' + style + '"/></w:pPr>' if style else ''
    return '<w:p>' + properties + '<w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>'


def docx(paragraphs, styles=None):
    members = [('word/document.xml', '<w:document xmlns:w="' + W + '"><w:body>' + paragraphs + '</w:body></w:document>')]
    if styles is not None:
        members.append(('word/styles.xml', '<w:styles xmlns:w="' + W + '">' + styles + '</w:styles>'))
    return package(members)


class ResultChecksTests(unittest.TestCase):
    def check_result(self, definition, content, expected='passed'):
        rule = C.parse_checks(definition)[0]
        if isinstance(content, str):
            content = content.encode('utf-8')
        result = C.evaluate_rule(rule, content)
        self.assertEqual(result['status'], expected, result)
        self.assertTrue(result['detail'])
        self.assertEqual(set(result), {'status', 'detail'})
        return result

    def test_empty_checks_keep_legacy_templates_unchanged(self):
        for value in ('', '  ', '\n\r\n\t'):
            self.assertEqual(C.parse_checks(value), [])

    def test_parse_all_kinds_and_fullwidth_delimiters(self):
        rules = C.parse_checks('必需列 ｜ 汇总.xlsx ｜ 汇总 ｜ 项目，金额\n列合计 | 汇总.csv | - | 金额 | 1200.00\n必需章节 | 周报.md | 进展,问题,下周计划')
        self.assertEqual(rules[0], dict(kind='required_columns', file='汇总.xlsx', sheet='汇总', columns=['项目', '金额']))
        self.assertEqual(rules[1], dict(kind='column_sum', file='汇总.csv', sheet='-', column='金额', expected='1200.00'))
        self.assertEqual(rules[2]['sections'], ['进展', '问题', '下周计划'])

    def test_invalid_definition_shapes_and_numeric_targets(self):
        invalid = [None, {}, 'unknown | a.csv | - | a', '必需列 | a.csv | -',
                   '必需列 | a.csv | - | ', '必需列 | a.csv | Sheet1 | a',
                   '必需列 | a.csv | - | a,,b', '必需列 | a.csv | - | a,a',
                   '列合计 | a.csv | - | a | NaN', '列合计 | a.csv | - | a | inf',
                   '列合计 | a.csv | - | a | =1+2', '列合计 | a.csv | - | a | 1,200',
                   '列合计 | a.csv | - | a | 1e1001', '列合计 | a.csv | - | a | ' + '1' * 129,
                   '必需列 | a.xls | - | a', '必需章节 | a.pdf | a',
                   '必需章节 | a.xlsx | a', '必需列 | a.docx | - | a',
                   '必需章节 | a.md | a,a', '必需章节 | a.md | a\x00b']
        for definition in invalid:
            with self.subTest(definition=definition), self.assertRaises(C.CheckDefinitionError):
                C.parse_checks(definition)

    def test_filename_must_be_exact_basename(self):
        for name in ('../a.csv', 'dir/a.csv', 'D:\\a.csv', 'a?.csv', '*.csv', 'a:1.csv', '.','..', 'a.csv.', 'a<1>.csv'):
            with self.subTest(name=name), self.assertRaises(C.CheckDefinitionError):
                C.parse_checks('必需列 | ' + name + ' | - | a')

    def test_definition_limits(self):
        for text in ('x' * 4001, '\n'.join(['必需列 | a.csv | - | a'] * 13),
                     '必需列 | a.csv | - | ' + ','.join(str(i) for i in range(21))):
            with self.assertRaises(C.CheckDefinitionError):
                C.parse_checks(text)
        self.assertEqual(len(C.parse_checks('\n'.join(['必需列 | a.csv | - | a'] * 12))), 12)

    def test_evaluation_of_invalid_rule_or_bytes_is_unverified(self):
        for rule in (None, {}, {'kind': 'required_sections', 'file': 'a.pdf', 'sections': ['a']},
                     {'kind': 'required_columns', 'file': 'a.csv', 'sheet': '-', 'columns': [1]}):
            self.assertEqual(C.evaluate_rule(rule, b'a')['status'], 'unverified')
        rule = C.parse_checks('必需列 | a.csv | - | a')[0]
        for content in (None, 'a', bytearray(b'a'), b'x' * (C.MAX_CONTENT_BYTES + 1)):
            self.assertEqual(C.evaluate_rule(rule, content)['status'], 'unverified')

    def test_csv_bom_blank_rows_and_quoted_fields(self):
        self.check_result('必需列 | a.csv | - | 项目,金额', '\ufeff\n  , \r\n 项目 , 金额 \r\n"line\nitem",12\r\n')
        self.check_result('列合计 | a.csv | - | 金额 | 3', '项目,金额\na,1\n, \nb,2\n\n')

    def test_csv_missing_and_duplicate_columns(self):
        self.check_result('必需列 | a.csv | - | 项目,金额', '项目,其他\na,1', 'failed')
        self.check_result('必需列 | a.csv | - | 金额', '项目,金额, 金额 \na,1,2', 'unverified')
        self.check_result('必需列 | a.csv | - | Amount', 'amount\n1', 'failed')
        self.check_result('必需列 | a.csv | - | 金额', '', 'failed')

    def test_csv_malformed_and_wrong_encoding(self):
        for data in (b'a\n"unterminated', b'a\n"closed"junk', b'\xff\xfea\x00', b'a\x00\n1'):
            self.check_result('必需列 | a.csv | - | a', data, 'unverified')

    def test_decimal_sum_is_exact_with_integer_decimal_scientific_values(self):
        for data, target in [('0.1\n0.2', '0.30'), ('1\n2\n-3', '0'), ('.5\n+1.50', '2'), ('1E-2\n2e-2', '.03')]:
            with self.subTest(data=data):
                self.check_result('列合计 | a.csv | - | 金额 | ' + target, '金额\n' + data)
        self.check_result('列合计 | a.csv | - | 金额 | .31', '金额\n.1\n.2', 'failed')

    def test_sum_rejects_missing_or_ambiguous_numeric_values(self):
        for data in ('项目,金额\na,', '项目,金额\na', '项目,金额\na,NaN', '项目,金额\na,=1+2',
                     '项目,金额\na,12元', '项目,金额\na,1e1001', '项目,金额\na,' + '1' * 129):
            with self.subTest(data=data):
                self.check_result('列合计 | a.csv | - | 金额 | 0', data, 'unverified')
        self.check_result('列合计 | a.csv | - | 金额 | 0', '金额\n\n', 'unverified')

    def test_csv_row_and_column_limits(self):
        with patch.object(C, 'MAX_ROWS', 2):
            self.check_result('必需列 | a.csv | - | a', 'a\n1\n2', 'unverified')
        with patch.object(C, 'MAX_COLUMNS', 2):
            self.check_result('必需列 | a.csv | - | a', 'a,b,c\n1,2,3', 'unverified')

    def test_xlsx_required_columns_and_exact_sum(self):
        data = xlsx()
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 项目,金额', data)
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | .3', data)
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | .4', data, 'failed')

    def test_xlsx_workbook_sheet_selection_is_exact(self):
        self.check_result('必需列 | 汇总.xlsx | 不存在 | 金额', xlsx(), 'failed')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(sheets='<sheet name="汇总" r:id="rId1"/><sheet name="汇总" r:id="rId2"/>'), 'unverified')

    def test_xlsx_duplicate_relationship_and_external_target_rejected(self):
        base = '<Relationship Id="rId1" Type="' + R + '/worksheet" Target="worksheets/sheet1.xml"/>'
        for rel in (base + base, base.replace('Target=', 'TargetMode="External" Target='),
                    base.replace('worksheets/sheet1.xml', '../elsewhere.xml'), base.replace('/worksheet', '/chartsheet')):
            self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(rel=rel), 'unverified')

    def test_xlsx_shared_strings_rich_text_and_phonetic_text(self):
        strings = '<sst xmlns="' + S + '"><si><r><t>金</t></r><r><t>额</t></r><rPh><t>ignored</t></rPh></si></sst>'
        data = xlsx(rows=row(1, cell('A1', '0', 's')) + row(2, cell('A2', '2', 'n')), extras=[('xl/sharedStrings.xml', strings)])
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 2', data)

    def test_xlsx_shared_string_bad_index_and_duplicate_columns(self):
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(rows=row(1, cell('A1', '99', 's'))), 'unverified')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(rows=row(1, cell('A1', '金额'), cell('B1', '金额'))), 'unverified')

    def test_xlsx_formula_cached_total_never_passes(self):
        for formula in ('<f>SUM(1,2)</f>', '<f t="shared" si="0"/>', '<f t="array" ref="A2:A2">1+2</f>'):
            data = xlsx(rows=row(1, cell('A1', '金额')) + row(2, cell('A2', '3', 'n', formula)))
            self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 3', data, 'unverified')
        data = xlsx(rows=row(1, cell('A1', '金额', 'str', '<f>"金额"</f>')) + row(2, cell('A2', '3', 'n')))
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', data, 'unverified')

    def test_xlsx_array_formula_cache_without_local_formula_is_unverified(self):
        data = xlsx(rows=row(1, cell('A1', '其他'), cell('B1', '金额')) + row(2, cell('A2', '1', 'n', '<f t="array" ref="A2:B2">SEQUENCE(1,2)</f>'), cell('B2', '2', 'n')))
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 2', data, 'unverified')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', data)

    def test_xlsx_unrelated_formula_does_not_block_target_numeric_column(self):
        data = xlsx(rows=row(1, cell('A1', '其他'), cell('B1', '金额')) + row(2, cell('A2', '1', 'n', '<f>1</f>'), cell('B2', '2', 'n')))
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 2', data)

    def test_xlsx_blank_error_boolean_and_unsupported_cell_types(self):
        for kind, value in [('n', ''), ('e', '#DIV/0!'), ('b', '1'), ('d', '2026-10-03'), ('bad', '2')]:
            data = xlsx(rows=row(1, cell('A1', '项目'), cell('B1', '金额')) + row(2, cell('A2', 'item'), cell('B2', value, kind)))
            self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 1', data, 'unverified')

    def test_xlsx_sparse_rows_blank_rows_and_missing_target_cells(self):
        data = xlsx(rows=row(1) + row(3, cell('A3', '金额')) + row(4) + row(8, cell('A8', '1', 'n')))
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 1', data)
        data = xlsx(rows=row(1, cell('A1', '项目'), cell('B1', '金额')) + row(2, cell('A2', 'item')))
        self.check_result('列合计 | 汇总.xlsx | 汇总 | 金额 | 0', data, 'unverified')

    def test_xlsx_duplicate_and_out_of_bounds_positions(self):
        for rows in (row(1, cell('A1', '金额')) + row(1, cell('A1', '1', 'n')),
                     row(1, cell('A1', '金额'), cell('A1', '金额')),
                     row(1, cell('A2', '金额')), row(20001, cell('A20001', '金额')),
                     row(1, cell('GS1', '金额'))):
            self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(rows=rows), 'unverified')

    def test_xlsx_duplicate_sheet_data_and_conflicting_cell_content(self):
        sheet = '<worksheet xmlns="' + S + '"><sheetData>' + row(1, cell('A1', '金额')) + '</sheetData><sheetData/></worksheet>'
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(sheet_xml=sheet), 'unverified')
        rows = row(1, '<c r="A1" t="inlineStr"><is><t>金额</t></is><v>2</v></c>')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(rows=rows), 'unverified')

    def test_bad_zip_and_xml_do_not_pass(self):
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', b'not a zip', 'unverified')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(sheet_xml='<broken'), 'unverified')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', package([]), 'unverified')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx()[:-20], 'unverified')

    def test_xml_dtd_and_entities_rejected_in_utf8_and_utf16(self):
        xml = '<?xml version="1.0"?><!DOCTYPE worksheet [<!ENTITY injected "金额">]><worksheet xmlns="' + S + '"><sheetData>' + row(1, '<c r="A1" t="inlineStr"><is><t>&injected;</t></is></c>') + '</sheetData></worksheet>'
        for encoded in (xml.encode('utf-8'), xml.replace('version="1.0"', 'version="1.0" encoding="UTF-16"').encode('utf-16')):
            self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(sheet_xml=encoded), 'unverified')

    def test_zip_unsafe_names_duplicates_symlink_and_limits(self):
        for name in ('../outside.xml', '/outside.xml', 'C:evil.xml', 'xl/workbook.xml'):
            with self.subTest(name=name):
                self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(extras=[(name, '<x/>')]), 'unverified')
        # Windows ZipInfo normalizes backslashes on write and read; mutate raw names.
        backwards = xlsx(extras=[('a/b.xml', '<x/>')]).replace(b'a/b.xml', b'a\\b.xml')
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', backwards, 'unverified')
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(extras=[(link, 'target')]), 'unverified')
        for constant, limit in [('MAX_ZIP_MEMBERS', 2), ('MAX_ZIP_MEMBER_BYTES', 16), ('MAX_ZIP_EXPANDED_BYTES', 32)]:
            with patch.object(C, constant, limit):
                self.check_result('必需列 | 汇总.xlsx | 汇总 | 金额', xlsx(), 'unverified')

    def test_markdown_atx_and_setext_are_actual_headings(self):
        self.check_result('必需章节 | 周报.md | 进展,问题,下周计划', '# 进展 ###\n正文\n\n问题\n===\n\n下周计划\n---\n')

    def test_markdown_substrings_body_fences_and_comments_are_not_headings(self):
        cases = ['正文讨论进展', '进展', '# 进展情况', '#进展', '    # 进展',
                 '```md\n# 进展\n```', '~~~\n# 进展\n~~~', '> # 进展',
                 '<!--\n# 进展\n-->', '<pre>\n# 进展\n</pre>', '<div>\n# 进展\n</div>',
                 '<![CDATA[\n# 进展\n]]>', '<?instruction\n# 进展\n?>',
                 '<custom>\n# 进展\n</custom>']
        for data in cases:
            self.check_result('必需章节 | 周报.md | 进展', data, 'failed')
        self.check_result('必需章节 | 周报.md | 进展', '```\n# 假标题\n```\n# 进展')

    def test_markdown_multiline_setext_is_one_complete_title(self):
        self.check_result('必需章节 | 周报.md | 进展', '正文\n进展\n---', 'failed')
        self.check_result('必需章节 | 周报.md | 正文 进展', '正文\n进展\n---')

    def test_txt_requires_complete_line_and_heading_duplicates_unverified(self):
        self.check_result('必需章节 | 周报.txt | 进展,问题', '\ufeff 进展 \n正文\n问题\n')
        self.check_result('必需章节 | 周报.txt | 进展', '本周进展很好', 'failed')
        for filename, text in [('周报.txt', '进展\n正文\n进展'), ('周报.md', '# 进展\n正文\n## 进展')]:
            self.check_result('必需章节 | ' + filename + ' | 进展', text, 'unverified')

    def test_docx_only_heading_styles_count(self):
        text = paragraph('进展', 'Heading1') + paragraph('正文提到下周计划') + paragraph('问题', '标题 2') + paragraph('下周计划', 'Heading9')
        self.check_result('必需章节 | 周报.docx | 进展,问题,下周计划', docx(text))
        for style in (None, 'Normal', 'Heading10', 'Title', '标题0'):
            self.check_result('必需章节 | 周报.docx | 进展', docx(paragraph('进展', style)), 'failed')

    def test_docx_style_name_resolution_and_duplicate_styles(self):
        style = '<w:style w:type="paragraph" w:styleId="custom"><w:name w:val="Heading 2"/></w:style>'
        self.check_result('必需章节 | 周报.docx | 进展', docx(paragraph('进展', 'custom'), style))
        self.check_result('必需章节 | 周报.docx | 进展', docx(paragraph('进展', 'custom'), style + style), 'unverified')
        self.check_result('必需章节 | 周报.docx | 进展', docx(paragraph('进展', 'Heading1') * 2), 'unverified')

    def test_docx_never_uses_body_substring_and_rejects_xml_entity(self):
        self.check_result('必需章节 | 周报.docx | 进展', docx(paragraph('本周进展', 'Heading1') + paragraph('进展')), 'failed')
        data = package([('word/document.xml', '<!DOCTYPE document [<!ENTITY x "进展">]><w:document xmlns:w="' + W + '"><w:body/></w:document>')])
        self.check_result('必需章节 | 周报.docx | 进展', data, 'unverified')

    def test_docx_heading_separators_cannot_collapse_into_matching_title(self):
        for separator in ('tab', 'br', 'cr'):
            body = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>进</w:t><w:' + separator + '/><w:t>展</w:t></w:r></w:p>'
            with self.subTest(separator=separator):
                self.check_result('必需章节 | 周报.docx | 进展', docx(body), 'failed')
        # Separate styled runs without separators do form one complete title.
        body = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>进</w:t></w:r><w:r><w:t>展</w:t></w:r></w:p>'
        self.check_result('必需章节 | 周报.docx | 进展', docx(body))

    def test_docx_nested_textbox_body_is_not_enclosing_heading_text(self):
        body = '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:drawing><w:txbxContent>' + paragraph('进展') + '</w:txbxContent></w:drawing></w:r></w:p>'
        self.check_result('必需章节 | 周报.docx | 进展', docx(body), 'failed')

    def test_missing_details_name_user_requirements_with_bounded_length(self):
        result = self.check_result('必需列 | a.csv | - | 项目,金额', '项目\na', 'failed')
        self.assertIn('金额', result['detail'])
        result = self.check_result('必需章节 | a.md | 进展,问题', '# 进展', 'failed')
        self.assertIn('问题', result['detail'])
        names = ','.join('章节' + str(i) + '长' * 100 for i in range(20))
        result = self.check_result('必需章节 | a.md | ' + names, '', 'failed')
        self.assertLessEqual(len(result['detail']), 300)
        self.assertIn('另有', result['detail'])

    def test_details_never_echo_file_contents(self):
        private_value = 'private-sensitive-document-fragment'
        result = self.check_result('列合计 | a.csv | - | 金额 | 1', '金额\n' + private_value, 'unverified')
        self.assertNotIn(private_value, result['detail'])
        result = self.check_result('必需章节 | a.md | 进展', '# ' + private_value, 'failed')
        self.assertNotIn(private_value, result['detail'])


if __name__ == '__main__':
    unittest.main()
