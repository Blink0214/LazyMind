"""Render one planned figure with bounded code repair and export checks."""
from __future__ import annotations

import hashlib
import os

from .code import validate_code
from .executor import PlotEnvironmentError, execute_plot
from .model import PlotModelError
from .templates import compose_native_program, validate_native_program
from ..analysis.validation import validate_charts
from ..analysis import trace as diagnostics


def limits() -> tuple[int, int]:
    try:
        attempts = int(os.environ.get('LAZYMIND_PAPER_PLOT_ATTEMPTS', '3'))
        if not 1 <= attempts <= 5:
            raise ValueError
    except ValueError as exc:
        raise ValueError('绘图尝试次数必须是 1..5') from exc
    try:
        concurrency = int(os.environ.get('LAZYMIND_PAPER_PLOT_CONCURRENCY', '4'))
        if not 1 <= concurrency <= 4:
            raise ValueError
    except ValueError as exc:
        raise ValueError('绘图并发数必须是 1..4') from exc
    return attempts, concurrency


def draw_one(bound: dict, measured: dict, spec: dict, reference: str, attempts: int,
             code_generator) -> tuple[dict, dict]:
    previous, error = '', ''
    history = []
    for attempt in range(1, attempts + 1):
        try:
            details = dict(figure_id=spec['id'], attempt=attempt)
            with diagnostics.stage('figure_code_generation', **details):
                previous = code_generator(bound, measured, spec, reference, previous, error)
                name = diagnostics.figure_name(spec['id'])
                diagnostics.save(f'figure-{name}-code-{attempt}.py', previous)
            with diagnostics.stage('figure_code_validation', **details):
                previous = compose_native_program(previous, reference, spec)
                validate_code(previous)
                validate_native_program(previous, spec)
            with diagnostics.stage('figure_execution', **details):
                rendered = execute_plot({'table': bound, 'statistics': measured, 'spec': spec,
                                         'data_sha256': measured['data_sha256'], 'code': previous})
            with diagnostics.stage('figure_validation', **details):
                if set(rendered) != {'svg', 'exports'}:
                    raise ValueError('绘图执行结果必须包含 svg/exports')
                chart = {'id': spec['id'], 'title': spec['title'], 'columns': spec['columns'], **rendered}
                if spec.get('caption'):
                    chart['caption'] = spec['caption']
                validate_charts([chart])
                diagnostics.save(f'figure-{name}-exports.json', {
                    'id': spec['id'], 'svg_bytes': len(rendered['svg'].encode()),
                    'formats': sorted(rendered['exports']),
                    'code_sha256': hashlib.sha256(previous.encode()).hexdigest(),
                })
            history.append({'attempt': attempt, 'status': 'passed_execution_and_file_checks'})
            trace = {'id': spec['id'], 'code': previous, 'data_contract': spec['data_contract'],
                     'caption': spec.get('caption', ''),
                     'style_contract': spec.get('style_contract', {}),
                     'code_sha256': hashlib.sha256(previous.encode()).hexdigest(), 'attempts': history,
                     'first_attempt_passed_execution_and_file_checks': attempt == 1}
            return chart, trace
        except (PlotEnvironmentError, PlotModelError):
            raise
        except (ValueError, TypeError, KeyError, SyntaxError, RuntimeError) as exc:
            current_error = str(exc)[-6000:]
            history.append({'attempt': attempt, 'status': 'failed', 'error': current_error,
                            'code_sha256': hashlib.sha256(previous.encode()).hexdigest()})
            error = current_error if len(history) == 1 else '\n'.join(
                f'Attempt {item["attempt"]}: {item["error"]}' for item in history)[-12000:]
    raise RuntimeError(f'图表 {spec["id"]} 在 {attempts} 次尝试后失败，未发布本轮图表: {error}')
