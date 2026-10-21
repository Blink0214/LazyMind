"""Expose native drawing functions without example data or file I/O."""
from __future__ import annotations

import ast
import copy
import re
from pathlib import Path

NATIVE_FUNCTION_GUIDANCE = '''Native drawing functions and their helpers are already injected by the host as globals.
Call their supplied names directly. Do not import skill modules or redefine these functions.
For example, call draw_point_intervals(...) directly; never use from paper_extended import draw_point_intervals.
Only third-party/standard modules in execution_rules.allowed_imports may be imported.
'''


def _called_functions(function, functions):
    """Resolve direct calls and simple local aliases/literal dispatch dictionaries."""
    aliases = {}

    def resolve(node):
        if isinstance(node, ast.Name):
            return {node.id} if node.id in functions else aliases.get(node.id, set())
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Dict):
            values = [resolve(value) for value in node.value.values]
            return set().union(*values) if values and all(values) else set()
        return set()

    assignments = [n for n in ast.walk(function) if isinstance(n, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            for target in targets:
                if sum(any(isinstance(t, ast.Name) and t.id == target for t in a.targets)
                       for a in assignments) != 1:
                    continue  # Reassigned names need runtime reasoning; don't guess.
                names = resolve(node.value)
                if names and names != aliases.get(target):
                    aliases[target] = names
                    changed = True
        if not changed:
            break
    return set().union(*(resolve(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)))


def compose_native_program(code: str, reference: str, spec: dict) -> str:
    """Load reviewed native helpers unchanged; model code supplies the data binding."""
    contract = spec.get('style_contract', {})
    if contract.get('version') not in (None, 'paper-v1'):
        raise ValueError('仅支持当前 paper-v1 绘图模板格式')
    if contract.get('version') != 'paper-v1' or spec.get('style') == 'custom':
        return code
    blocks = re.findall(r'```python\s*\n([\s\S]*?)```', reference)
    if not blocks:
        raise ValueError('原生论文图缺少可加载的 skill 模板代码')
    template = blocks[-1]
    reserved = {n.name for n in ast.parse(template).body if isinstance(n, ast.FunctionDef)}
    if contract['native_template'] not in reserved:
        raise ValueError('skill 模板缺少声明的原生入口')
    adapters = ''
    if spec.get('data_contract', {}).get('result_view_version') == 1:
        adapters = Path(__file__).with_name('result_api.py').read_text()
        reserved.update(n.name for n in ast.parse(adapters).body if isinstance(n, ast.FunctionDef))
    bindings = []
    for node in ast.parse(code).body:
        if (isinstance(node, ast.ImportFrom) and not node.level
                and node.module in {contract.get('helper_module'), 'paper_common'}):
            invalid = [alias.name for alias in node.names
                       if alias.name not in reserved or alias.asname not in (None, alias.name)]
            if invalid:
                raise ValueError(f'skill 模块 {node.module} 的导入包含未提供函数或别名: {", ".join(invalid)}；'
                                 f'删除该导入，直接使用已注入的函数名: {", ".join(sorted(reserved))}')
            # These exact reviewed definitions are appended below; no module is loaded.
            continue
        if not isinstance(node, ast.FunctionDef) or node.name not in reserved:
            bindings.append(node)
    return ast.unparse(ast.Module(body=bindings, type_ignores=[])) + '\n\n' + template + '\n\n' + adapters


def validate_native_program(code: str, spec: dict) -> None:
    """A native grouped-bar plan must actually use its supplied drawing function, not silently discard it."""
    contract = spec.get('style_contract', {})
    if contract.get('version') not in (None, 'paper-v1'):
        raise ValueError('仅支持当前 paper-v1 绘图模板格式')
    if contract.get('version') == 'paper-v1' and spec.get('style') != 'custom':
        tree = ast.parse(code)
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        name = contract['native_template']
        pending, reached = ['draw'], set()
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            if current in functions:
                pending.extend(_called_functions(functions[current], functions))
        if name not in functions or name not in reached:
            raise ValueError(f'原生论文图必须在 draw 的调用路径中定义并调用 {name}；'
                             '复制提供的绘制函数并绑定实际数据，不能丢弃模板只保留配色')
        return


def paper_template(script: str, common: str, source_name: str, target_name: str, extra_names: tuple = ()) -> str:
    """Extract only named drawing functions; no example globals, imports or top-level execution."""
    output = []
    defined = {n.name: n for n in ast.parse(script).body if isinstance(n, ast.FunctionDef)}
    pending, selected = [source_name, *extra_names], []
    while pending:
        name = pending.pop(0)
        if name in selected:
            continue
        if name not in defined:
            raise ValueError(f'论文 skill 缺少唯一绘图函数 {name}')
        selected.append(name)
        pending.extend(node.func.id for node in ast.walk(defined[name])
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id in defined)
    for source, names in [(common, ['paper_axes', 'paper_legend']), (script, selected)]:
        nodes = ast.parse(source).body
        for name in names:
            matches = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name]
            if len(matches) != 1:
                raise ValueError(f'论文 skill 缺少唯一绘图函数 {name}')
            function = copy.deepcopy(matches[0])
            if name == source_name:
                function.name = target_name
            output.append(ast.unparse(function))
    return ('import numpy as np\nimport matplotlib.pyplot as plt\nimport matplotlib.patches as mpatches\n'
            'import matplotlib.colors as mcolors\n\n'
            + '\n\n'.join(output))
