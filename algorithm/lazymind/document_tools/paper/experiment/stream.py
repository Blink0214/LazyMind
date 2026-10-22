"""Project F3 model deltas onto existing task logs and attempt-scoped artifact previews."""
import json
import time

from json_repair import loads as repair_loads

from lazymind.document_tools.writing import DraftMarkdownStreamEventEmitter
from .schema import ExperimentDraft, Preview


class _PreviewEmitter(DraftMarkdownStreamEventEmitter):
    # The existing Writer UI paints one delta per animation frame.
    MAX_DELTA_CHARS = 1


class ExperimentStream:
    def __init__(self, emit, topic, slots):
        self.emit = emit
        self.topic = topic
        self.slots = set(slots)
        self.streams = {}
        self.contents = {}
        self.buffer = ''
        self.think = ''
        self.last_parse = 0.0
        self.last_think = time.monotonic()
        self.last_phase = ''
        self.progress = 0
        self.phase('正在设计实验并生成方案', 5)

    def phase(self, text, progress):
        if progress >= self.progress and text != self.last_phase:
            self.progress = progress
            self.last_phase = text
            self.emit({'type': 'progress', 'progress': progress, 'current_phase': text})
            self.emit({'type': 'text', 'text': text + '\n'})

    def __call__(self, event):
        tag, delta = event.get('tag'), event.get('delta')
        if tag == 'phase':
            self.flush_think()
            self.phase(event['phase'], 85)
        elif tag == 'think' and isinstance(delta, str):
            self.think += delta
            if len(self.think) >= 512 or time.monotonic() - self.last_think >= 0.3:
                self.flush_think()
        elif tag == 'text' and isinstance(delta, str):
            self.flush_think()
            self.buffer += delta
            if len(self.buffer) > 1_000_000:
                raise ValueError('实验模型输出超过 1 MB，已停止生成')
            if time.monotonic() - self.last_parse >= 0.4:
                self.last_parse = time.monotonic()
                self.preview()

    def flush_think(self):
        if self.think:
            self.emit({'type': 'think', 'think': self.think})
            self.think = ''
            self.last_think = time.monotonic()

    def publish(self, slot, text):
        if slot not in self.slots or not text or self.contents.get(slot) == text:
            return
        previous = self.contents.get(slot, '')
        if slot not in self.streams:
            self.streams[slot] = _PreviewEmitter(self.emit, slot=slot)
        stream = self.streams[slot]
        if text.startswith(previous):
            stream.feed(text[len(previous):])
        else:
            # Partial JSON can revise its trailing escape/number. Replace, never concatenate stale content.
            stream.restart(text)
        self.contents[slot] = text

    def preview(self):
        try:
            candidate = repair_loads(self.buffer, stream_stable=True)
        except (ValueError, TypeError, IndexError):
            return
        if not isinstance(candidate, dict):
            return
        candidate = ExperimentDraft.discard_extra_model_fields(candidate)
        plan, explanation = candidate.get('plan'), candidate.get('explanation')
        if isinstance(plan, str) and plan:
            text = f'# 实验研究 · {self.topic}\n\n> 生成中的预览，尚未完成校验；未运行实验。\n\n## 实验方案\n{plan}'
            if isinstance(explanation, str) and explanation:
                text += '\n\n## 代码与运行说明\n' + explanation
                self.phase('正在生成实验代码与运行说明', 30)
            self.publish('experiment_document', text)
        files = candidate.get('code_files')
        if isinstance(files, list):
            languages = {'experiment.py': 'python', 'config.json': 'json',
                         'requirements.txt': 'text', 'README.md': 'markdown'}
            parts = []
            for item in files:
                if isinstance(item, dict) and item.get('path') in languages and isinstance(item.get('content'), str):
                    name = item['path']
                    parts.append(f'## {name}\n```{languages[name]}\n' + item['content'])
            if parts:
                self.phase('正在生成实验代码包', 50)
                self.publish('experiment_code', '# 实验代码（预览，未执行）\n\n' + '\n```\n\n'.join(parts))
        if isinstance(candidate.get('results'), dict):
            try:
                table = Preview.model_validate(candidate['results']).table().model_dump()
            except ValueError:
                return
            self.phase('正在整理模拟结果与来源说明', 75)
            self.publish('experiment_results', '# 模拟结果（预览，未执行）\n\n```json\n'
                         + json.dumps(table, ensure_ascii=False, indent=2) + '\n```\n')

    def validated(self, output):
        self.flush_think()
        self.publish('experiment_document', output['experiment']['report'])
        table = output['experiment']['results']
        if table is not None:
            from .handoff import source_notice
            preview = {**table, 'rows': table['rows'][:20]}
            self.publish('experiment_results', '# 实验结果\n\n' + source_notice(output['result_meta']) + '\n\n```json\n'
                         + json.dumps(preview, ensure_ascii=False, indent=2) + '\n```\n'
                         + f'共 {len(table["rows"])} 行，以上展示前 20 行；完整数据见结果文件。\n')
        self.phase('校验通过，正在保存实验方案、代码包和结果', 95)

    def close(self, error=None):
        self.flush_think()
        for stream in self.streams.values():
            if error is not None:
                stream.abort('本次生成未完成校验或保存，预览已作废：' + str(error)[:300])
            else:
                stream.end()
