"""Pure journal path rules shared by the writer and read-only desktop reader."""
from pathlib import Path


DEFAULT_JOURNAL_FILE = '.runtime/tasks.sqlite'


def resolve_journal_path(root, value):
    """An absent/disabled value means no journal; relative paths use project root."""
    if value is None or value is False or value == '':
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError('task_journal_file must be a path or disabled')
    path = Path(value)
    return (Path(root) / path).resolve()
