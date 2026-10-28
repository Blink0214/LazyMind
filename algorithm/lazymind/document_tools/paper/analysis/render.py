"""Export the authored analysis without appending host-generated result inventories."""


def render(analysis, title='数据分析'):
    return f'# {title}\n\n{analysis["summary"]}'
