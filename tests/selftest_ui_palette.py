"""WinForms palette: shared tokens only, readable text colors and no dangling names.

The UI scripts cannot run outside Windows, so these source checks catch the two
mistakes that would otherwise surface only on a real desktop:
- a misspelled ``[TrayDesign]::Name`` (PowerShell fails at runtime, not at load);
- a window reintroducing its own hard-coded or low-contrast text color.
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / 'desktop'
THEME = DESKTOP / 'tray_theme.cs'
TEXT_TOKENS = ('Blue', 'Heading', 'TextColor', 'MutedColor', 'SuccessText',
               'WarningText', 'DangerText', 'StandbyText')


def ui_scripts():
    return sorted(path for path in DESKTOP.glob('*.ps1')
                  if 'TrayDesign' in path.read_text(encoding='utf-8-sig'))


def palette():
    colors = {}
    source = THEME.read_text(encoding='utf-8')
    for name, r, g, b in re.findall(
            r'public static readonly Color (\w+)=Color\.FromArgb\((\d+),(\d+),(\d+)\)', source):
        colors[name] = (int(r), int(g), int(b))
    for name in re.findall(r'public static readonly Color (\w+)=Color\.White;', source):
        colors[name] = (255, 255, 255)
    return colors


def contrast(a, b):
    def luminance(rgb):
        def channel(value):
            c = value / 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (channel(v) for v in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class PaletteTests(unittest.TestCase):
    def test_ui_scripts_are_found(self):
        names = {path.name for path in ui_scripts()}
        self.assertTrue({'tray_app.ps1', 'workflow_window.ps1', 'task_history.ps1'} <= names, names)

    def test_every_referenced_theme_member_exists(self):
        source = THEME.read_text(encoding='utf-8')
        members = {}
        for block in re.split(r'\n(?=public (?:static )?class )', source):
            name = re.match(r'public (?:static )?class (\w+)', block)
            if name:
                members[name.group(1)] = set(re.findall(
                    r'public static (?:readonly )?[\w<>\[\]]+ (\w+)\s*[=(;]', block))
        self.assertIn('TrayDesign', members)
        for path in ui_scripts():
            for cls, member in set(re.findall(r'\[(Tray\w+)\]::(\w+)', path.read_text(encoding='utf-8-sig'))):
                if cls == 'TrayNative':   # declared inline in tray_app.ps1
                    continue
                self.assertIn(cls, members, '%s uses unknown class %s' % (path.name, cls))
                self.assertIn(member, members[cls], '%s uses %s::%s' % (path.name, cls, member))

    def test_windows_use_shared_tokens_instead_of_literal_colors(self):
        literal = re.compile(r"FromHtml\('#[0-9A-Fa-f]{6}'\)|\[Drawing\.Color\]::SlateGray")
        for path in ui_scripts():
            hits = literal.findall(path.read_text(encoding='utf-8-sig'))
            self.assertEqual(hits, [], path.name + ' should use [TrayDesign]:: colors')

    def test_text_tokens_are_readable_on_cards_and_page(self):
        colors = palette()
        for name in TEXT_TOKENS + ('CardColor', 'PageColor', 'BlueSoft'):
            self.assertIn(name, colors)
        for name in TEXT_TOKENS:
            for surface in ('CardColor', 'PageColor'):
                ratio = contrast(colors[name], colors[surface])
                self.assertGreaterEqual(ratio, 4.5, '%s on %s is %.2f:1' % (name, surface, ratio))
        self.assertGreaterEqual(contrast((255, 255, 255), colors['Blue']), 4.5, 'white text on primary')
        self.assertGreaterEqual(contrast(colors['Blue'], colors['BlueSoft']), 4.5, 'selected chip text')


if __name__ == '__main__':
    unittest.main()
