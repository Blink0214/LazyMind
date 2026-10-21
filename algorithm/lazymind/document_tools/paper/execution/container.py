"""Run a bounded, sandboxed Python child inside the existing Chat container."""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


class ContainerEnvironmentError(RuntimeError):
    """A deployment problem; never send this to the model for code repair."""


def validate_environment():
    if sys.platform != 'linux' or not any(Path(p).exists() for p in ('/.dockerenv', '/run/.containerenv')):
        raise ContainerEnvironmentError('container 执行后端必须运行在 Linux 服务容器内，不允许宿主进程回退')
    missing = [name for name in ('numpy', 'scipy', 'pandas', 'matplotlib', 'seaborn')
               if importlib.util.find_spec(name) is None]
    if missing:
        raise ContainerEnvironmentError('Chat 镜像缺少科学计算依赖，请重建镜像: ' + ', '.join(missing))
    try:
        seccomp = ctypes.CDLL('libseccomp.so.2')
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        number = seccomp.seccomp_syscall_resolve_name(b'landlock_create_ruleset')
        libc = ctypes.CDLL(None, use_errno=True)
        if number < 0 or libc.syscall(number, 0, 0, 1) < 3:
            raise OSError('Landlock ABI >= 3 required')
    except OSError as exc:
        raise ContainerEnvironmentError('容器内执行需要 libseccomp2 和支持 Landlock ABI >= 3 的 Linux 内核') from exc


def execute(payload, worker: Path, timeout: int, *, label: str) -> dict:
    """Keep only stdin/stdout/stderr open; never inherit Chat credentials or Python state."""
    launcher = Path(__file__).with_name('sandbox.py').resolve()
    with tempfile.TemporaryDirectory(prefix='lazymind-paper-') as directory:
        root = Path(directory)
        root.chmod(0o711)
        scratch = root / 'work'
        scratch.mkdir(mode=0o700)
        # The child drops root before executing generated code. Other jobs' scratch is private.
        if os.geteuid() == 0:
            os.chown(scratch, 65534, 65534)
        paths = [root / name for name in ('input.json', 'stdout', 'stderr')]
        paths[0].write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(scratch), 'TMPDIR': str(scratch),
               'MPLCONFIGDIR': str(scratch / 'matplotlib'), 'MPLBACKEND': 'Agg',
               'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
               'NUMEXPR_NUM_THREADS': '1', 'LANG': 'C.UTF-8'}
        command = [sys.executable, '-I', '-B', str(launcher), str(worker.resolve()), str(scratch), str(timeout)]
        process = None
        try:
            with paths[0].open('rb') as source, paths[1].open('wb') as out, paths[2].open('wb') as err:
                process = subprocess.Popen(command, stdin=source, stdout=out, stderr=err, env=env,
                                           cwd=scratch, close_fds=True, start_new_session=True)
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'{label}执行超时（{timeout} 秒）')
                    if any(p.stat().st_size > 16 * 1024 * 1024 for p in paths[1:]):
                        raise RuntimeError(f'{label}输出超过 16 MiB')
                    time.sleep(0.05)
            if any(p.stat().st_size > 16 * 1024 * 1024 for p in paths[1:]):
                raise RuntimeError(f'{label}输出超过 16 MiB')
            stderr = paths[2].read_text(errors='replace')[-3000:]
            if process.returncode == 78:
                raise ContainerEnvironmentError('容器内 Python 沙箱启动失败: ' + stderr)
            try:
                result = json.loads(paths[1].read_text())
            except ValueError as exc:
                raise RuntimeError(f'{label}没有返回有效 JSON: ' + stderr) from exc
            if not isinstance(result, dict) or process.returncode or 'error' in result:
                detail = result.get('error', stderr) if isinstance(result, dict) else stderr
                raise RuntimeError(f'{label}执行失败: ' + str(detail)[-6000:])
            return result
        except OSError as exc:
            raise ContainerEnvironmentError('容器内 Python 执行器不可用: ' + str(exc)) from exc
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
