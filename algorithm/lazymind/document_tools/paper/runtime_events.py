"""Internal, execution-scoped paper progress; never part of business inputs."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from threading import RLock

from ..writing import DraftMarkdownStreamEventEmitter

_current = ContextVar('paper_runtime_events', default=None)
_log = logging.getLogger(__name__)


class PaperRuntimeEvents:
    def __init__(self, emit, slot):
        self._emit = emit
        self._slot = slot
        self._preview = None
        self._progress = 0
        self._last = None
        self._closed = False
        self._lock = RLock()

    def progress(self, percent, phase):
        with self._lock:
            if self._closed:
                return
            self._progress = max(self._progress, min(100, int(percent)))
            state = (self._progress, phase)
            if state == self._last:
                return
            self._last = state
            try:
                self._emit({'type': 'progress', 'progress': self._progress, 'current_phase': phase})
            except Exception:
                _log.warning('Paper progress delivery failed', exc_info=True)

    def section_progress(self, event):
        total = int(event.get('section_total') or 0)
        done = int(event.get('section_completed') or 0)
        percent = 30 + int(50 * min(done, total) / total) if total else 30
        phase = f'正在生成正文，已完成 {done}/{total} 章' if total else '正在生成正文'
        self.progress(percent, phase)

    def delta(self, text):
        with self._lock:
            if self._closed or not text:
                return
            if self._preview is None:
                self._preview = DraftMarkdownStreamEventEmitter(self._emit, slot=self._slot)
            self._preview.feed(text)

    def restart(self, prefix):
        with self._lock:
            if self._closed:
                return
            if self._preview is None:
                self.delta(prefix)
            else:
                self._preview.restart(prefix)

    def complete(self):
        with self._lock:
            if self._preview:
                self._preview.end()
            self.progress(100, '大纲已保存，等待确认' if self._slot == 'write_outline_document' else '成稿已保存')
            self._closed = True

    def abort(self):
        with self._lock:
            if self._preview:
                self._preview.abort('写作未完成，预览已撤销；请查看任务错误。')
            self._closed = True


def current_events():
    return _current.get()


def report_progress(percent, phase):
    events = current_events()
    if events is not None:
        events.progress(percent, phase)


@contextmanager
def bind_runtime_events(emit, slot):
    events = PaperRuntimeEvents(emit, slot)
    token = _current.set(events)
    try:
        yield events
    except BaseException:
        events.abort()
        raise
    finally:
        _current.reset(token)
