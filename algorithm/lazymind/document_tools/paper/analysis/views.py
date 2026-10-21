"""Independent, bounded join materialization with original-row provenance."""
from .schema import table_map


def materialize(inputs, joins):
    sources = table_map(inputs)
    tables = dict(sources)
    lineage = {key: [{key: i} for i in range(len(table['rows']))] for key, table in sources.items()}
    dropped = {}
    total_cells = 0
    for join in joins:
        identifier, left_id, right_id = join['id'], join['left_table'], join['right_table']
        if identifier in tables or left_id == right_id or left_id not in sources or right_id not in sources:
            raise ValueError('关联视图 ID 重复，或引用了不存在/派生的输入表')
        left, right = sources[left_id], sources[right_id]
        if len(join['left_keys']) != len(join['right_keys']) or not join['left_keys']:
            raise ValueError('关联键必须非空且长度相同')
        li, ri = [], []
        for lk, rk in zip(join['left_keys'], join['right_keys']):
            if lk not in left['columns'] or rk not in right['columns']:
                raise ValueError('关联键不存在')
            a, b = left['columns'].index(lk), right['columns'].index(rk)
            lt, rt = left['column_types'][a], right['column_types'][b]
            if lt != rt and {lt, rt} != {'integer', 'number'}:
                raise ValueError('关联键类型不兼容')
            li.append(a)
            ri.append(b)
        if len(set(li)) != len(li) or len(set(ri)) != len(ri):
            raise ValueError('关联键重复')
        index = {}
        for row_id, row in enumerate(right['rows']):
            key = tuple(row[i] for i in ri)
            if None in key:
                continue
            if key in index:
                raise ValueError('当前关联要求右表键唯一，不能扩增记录制造样本')
            index[key] = row_id
        columns = [f'{left_id}.{c}' for c in left['columns']] + [f'{right_id}.{c}' for c in right['columns']]
        if len(set(columns)) != len(columns):
            raise ValueError('关联后列名有歧义')
        rows, refs, missing = [], [], 0
        for left_row_id, row in enumerate(left['rows']):
            key = tuple(row[i] for i in li)
            right_row_id = index.get(key) if None not in key else None
            if right_row_id is None:
                missing += 1
                if join['how'] == 'inner':
                    continue
                values, ref = [None] * len(right['columns']), {left_id: left_row_id}
            else:
                values, ref = right['rows'][right_row_id], {left_id: left_row_id, right_id: right_row_id}
            rows.append([*row, *values])
            refs.append(ref)
            total_cells += len(columns)
            if total_cells > 1_000_000:
                raise ValueError('关联视图累计超过 100 万单元格')
        if not rows:
            raise ValueError('关联后没有观测记录')
        tables[identifier] = dict(columns=columns, column_types=left['column_types'] + right['column_types'],
                                  units=left['units'] + right['units'], rows=rows)
        lineage[identifier] = refs
        dropped[identifier] = missing if join['how'] == 'inner' else 0
    return tables, lineage, dropped
