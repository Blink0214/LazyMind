"""Explicit public F4 milestones and response previews in a bounded public snapshot."""
import copy
import json
import re
import time

PUBLIC_STAGES = {
    'input_binding', 'input_validation', 'environment_validation', 'plan_generation', 'plan_repair',
    'figure_plan_repair', 'analysis_code_generation', 'analysis_execution', 'numerical_verification',
    'figure_code_prefetch', 'figure_code_generation', 'figure_execution', 'figure_validation',
    'interpretation', 'output_publication',
}
MAX_OUTPUT = 96 * 1024


def public_error(error):
    """Expose bounded contract messages, never exception causes or raw trace payloads."""
    from .trace import redact
    if not isinstance(error, dict):
        return None
    code = error.get('code', '')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z_]{0,63}', code):
        return dict(code='EXECUTION_FAILED', message='本次执行失败；诊断已保存，请检查执行记录。')
    message = str(error.get('message', '执行失败'))
    message = re.sub(r'input_value=.*?(?=\n|$)', 'input_value=<omitted>', message)
    message = re.sub(r'\s*For further information visit https?://[^\s]+', '', message)
    message = re.sub(r'\s*\[type=[^\]]+\]', '', message)
    return dict(code=code, field=str(error.get('field', ''))[:160], message=redact(message)[:1200])


class PublicProgress:
    def __init__(self, recorder):
        self.recorder = recorder
        self.steps = {}
        self.last_sent = {}
        self.figures = {}

    def send(self, identifier, force=True):
        if identifier not in self.steps:
            return
        now = time.monotonic()
        if not force and now - self.last_sent.get(identifier, 0) < 1:
            return
        step = self.steps[identifier]
        step['revision'] += 1
        self.last_sent[identifier] = now
        self.flush()

    def flush(self):
        from .trace import timestamp
        steps = copy.deepcopy(list(self.steps.values()))
        remaining = 512 * 1024
        for step in reversed(steps):
            if 'output' in step:
                output = step['output']
                raw = output['text'].encode('utf-8')
                output['text'] = raw[:remaining].decode('utf-8', errors='ignore')
                output['truncated'] = output.get('truncated', False) or len(raw) > remaining
                remaining = max(0, remaining - len(raw))
        summary = self.recorder.summary
        error = public_error(summary.get('error'))
        self.recorder._guard(lambda: self.recorder._write_json(self.recorder.root / 'public.json', dict(
            schema_version=1, task_id=summary['task_id'], execution_id=summary['trace_id'],
            revision=sum(s['revision'] for s in steps), process_steps=steps, error=error,
            timing=dict(started_at=summary['started_at'], finished_at=summary.get('ended_at'), measured_at=timestamp()))))

    def owner(self, item):
        while item and item['id'] not in self.steps:
            item = next((s for s in self.recorder.summary['stages'] if s['id'] == item.get('parent_id')), None)
        return item['id'] if item else None

    def begin(self, item, label):
        if item['stage'] in PUBLIC_STAGES and item.get('parent_id') is None:
            suffix = (f" · {self.figures.get(item['figure_id'], item['figure_id'])}" if item.get('figure_id') else '')
            suffix += f" · 第 {item['attempt']} 次" if item.get('attempt') else ''
            self.steps[item['id']] = dict(step_id='f4-' + item['id'], revision=0, order=len(self.steps),
                title=(label + suffix)[:100], status='running', summary='', started_at=item['started_at'],
                finished_at=None, elapsed_ms=None)
            self.send(item['id'])
        elif item['stage'] == 'model_transport_attempt':
            owner = self.owner(item)
            if owner:
                attempt = item.get('attempt', 1)
                self.steps[owner]['summary'] = ('等待模型返回内容' if attempt == 1 else
                    f'前次请求尚未返回内容且连接已断开，正在第 {attempt} 次请求；已完成的步骤保留。')
                self.send(owner)

    def end(self, item, exc):
        identifier = item['id']
        if identifier not in self.steps:
            return
        step = self.steps[identifier]
        step.update(status='failed' if exc else 'succeeded', finished_at=item['ended_at'],
                    elapsed_ms=item['duration_ms'])
        if exc:
            from .trace import safe_error
            step['summary'] = public_error(safe_error(exc))['message']
        elif step['summary'].startswith(('等待模型', '首次请求', '前次请求', '正在接收模型', '模型返回已结束', '模型完整结果')):
            step['summary'] = '本阶段已完成。'
        self.send(identifier)

    def note(self, name, value):
        if name == 'locked-plan.json':
            self.figures = {f['id']: f.get('title', f['id']) for f in value.get('figures', [])}
        stages = ({'input_validation'} if name == 'input-summary.json' else
                  {'plan_generation', 'plan_repair', 'figure_plan_repair'} if name == 'locked-plan.json' else set())
        identifier = next((k for k in reversed(self.steps)
                           if next(s for s in self.recorder.summary['stages'] if s['id'] == k)['stage'] in stages), None)
        if not identifier:
            return
        if name == 'input-summary.json':
            tables = value.get('tables', [])
            summary = f"已读取 {sum(t['rows'] for t in tables)} 行数据、{len(tables)} 张数据表，输入校验通过。"
        else:
            summary = f"计划已确定：{len(value.get('jobs', []))} 项统计计算、{len(value.get('figures', []))} 张图。"
        self.steps[identifier]['summary'] = summary
        self.send(identifier)


class ResponsePreview:
    """Only the provider's formal text channel; leading think envelopes are withheld."""
    def __init__(self, recorder, parent_id, json_output):
        self.recorder = recorder
        item = next((s for s in recorder.summary['stages'] if s['id'] == parent_id), None)
        self.owner = recorder.public.owner(item)
        self.buffer = ''
        self.text = ''
        self.leading = True
        self.inside = None
        self.received_text = False
        self.truncated = False
        self.format = 'json' if json_output else 'python'

    def _publish(self, mode, force=False):
        if not self.owner:
            return
        from .trace import redact
        with self.recorder.lock:
            first = 'output' not in self.recorder.public.steps[self.owner]
            visible = redact(self.text).encode('utf-8')[:MAX_OUTPUT].decode('utf-8', errors='ignore')
            self.recorder.public.steps[self.owner]['output'] = dict(
                text=visible, format=self.format, mode=mode,
                truncated=self.truncated or len(self.text.encode('utf-8')) > MAX_OUTPUT)
            self.recorder.public.steps[self.owner]['summary'] = (
                '正在接收模型返回的正文' if mode == 'streaming' else
                '模型完整结果已返回，可分段查看。' if mode == 'buffered' else
                '本次模型返回已中断，阶段结果尚未确认。' if mode == 'interrupted' else
                '模型返回已结束，后续将解析与校验。')
            self.recorder.public.send(self.owner, force or first)

    def sink(self, payload):
        if payload.get('tag') != 'text' or not payload.get('delta'):
            return
        self.received_text = True
        self.buffer += str(payload['delta'])
        while self.leading:
            if self.inside:
                match = re.search(r'</' + self.inside + r'>', self.buffer, re.I)
                if not match:
                    # Keep just the possible split closing tag, never the hidden body.
                    self.buffer = self.buffer[-16:]
                    return
                self.buffer = self.buffer[match.end():]
                self.inside = None
            stripped = self.buffer.lstrip()
            if not stripped:
                return
            lower = stripped.lower()
            tags = ('<think>', '<thinking>')
            if any(tag.startswith(lower) for tag in tags):
                return
            match = re.match(r'<(think|thinking)>', stripped, re.I)
            if match:
                self.inside = match[1]
                self.buffer = stripped[match.end():]
                continue
            self.leading = False
            self.buffer = stripped
        self.truncated = self.truncated or len(self.text) + len(self.buffer) > MAX_OUTPUT
        if len(self.text) < MAX_OUTPUT:
            self.text += self.buffer[:MAX_OUTPUT - len(self.text)]
        self.buffer = ''
        self._publish('streaming')

    def finish(self, raw):
        from ..plotting.code_response import response_text
        # Display normalization does not change the value returned to the algorithm.
        text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else response_text(raw or '')[0]
        self.truncated = len(text.encode('utf-8')) > MAX_OUTPUT
        self.text = text[:MAX_OUTPUT]
        self._publish('complete' if self.received_text and not self.leading else 'buffered', force=True)

    def abort(self):
        self._publish('interrupted', force=True)
