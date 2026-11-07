"""A string-compatible result whose status does not depend on display text."""


class RunnerResult(str):
    def __new__(cls, text, state='completed'):
        if state not in ('completed', 'failed'):
            raise ValueError(state)
        value = super().__new__(cls, text)
        value.state = state
        return value


def result_failed(value):
    # Old/custom runners still return the legacy error string. Production
    # runners return RunnerResult, including successful text beginning with ❌.
    if isinstance(value, RunnerResult):
        return value.state == 'failed'
    return str(value or '').startswith('❌')
