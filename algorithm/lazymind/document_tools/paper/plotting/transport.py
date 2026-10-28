"""Per-request F4 client controls, never provider API generation parameters."""
from dataclasses import dataclass
import os
import time

from ..analysis import trace as diagnostics


@dataclass(frozen=True)
class Policy:
    connect: int = 15
    read: int = 600
    attempts: int = 2


def configuration():
    values = {}
    for name, default, upper in [('CONNECT_TIMEOUT', 15, 120), ('READ_TIMEOUT', 600, 3600),
                                  ('MAX_ATTEMPTS', 2, 3)]:
        try:
            value = int(os.environ.get('LAZYMIND_PAPER_MODEL_' + name, str(default)))
            if not 1 <= value <= upper:
                raise ValueError
        except ValueError as exc:
            raise ValueError('F4 模型客户端配置无效: ' + name) from exc
        values[name] = value
    return Policy(values['CONNECT_TIMEOUT'], values['READ_TIMEOUT'], values['MAX_ATTEMPTS'])


def configure(client, policy):
    from lazyllm.module.llms.onlinemodule.chat import OnlineChatModule
    from lazyllm.module.llms.onlinemodule.base.onlineChatModuleBase import LazyLLMOnlineChatModuleBase

    supported = isinstance(client, (OnlineChatModule, LazyLLMOnlineChatModuleBase))
    if supported:
        # AutoModel creates this client for this request. Dynamic routers pass
        # their constructor timeout to each supplier when it is first created.
        # Passing timeout in forward kwargs would also put it in the API body.
        client._timeout = (policy.connect, policy.read)
        # Keep the provider's content/reasoning fields separate for this F4 call.
        # The SDK's default formatter concatenates them into <think> text.
        client.formatter(lambda message: message)
        if isinstance(client, LazyLLMOnlineChatModuleBase):
            supplier = client
        else:
            from lazyllm import globals
            with globals.stack_enter(client.identities):
                supplier = client._get_supplier()
        if supplier.PROVIDER_NAME == 'minimax':
            supplier._model_optional_params['reasoning_split'] = True
            # This F4 request reads native fields; bypass the SDK's lossy details
            # conversion on this newly created instance only.
            supplier._convert_msg_format = lambda message: message
    return supported


def retry_connection(exc):
    """Only reconnect before a response; never repeat a long read timeout."""
    terminal = getattr(exc, 'terminal', None)
    failure = getattr(terminal, 'failure', None)
    if (failure is None or getattr(failure, 'response_started', True)
            or getattr(terminal, 'has_semantic_output', True)):
        return False
    if getattr(getattr(failure, 'origin', None), 'value', None) != 'transport':
        return False
    names = {type(item).__name__ for item in diagnostics.exception_chain(exc)}
    if names & {'ReadTimeout', 'ReadTimeoutError', 'Timeout', 'SSLError', 'ProxyError'}:
        return False
    return bool(names & {'ConnectTimeout', 'ConnectTimeoutError', 'ConnectionError', 'NewConnectionError',
                         'ConnectionResetError', 'ConnectionAbortedError', 'RemoteDisconnected'})


def call(client, prompt, options, policy, supported):
    attempts = policy.attempts if supported else 1
    for attempt in range(1, attempts + 1):
        try:
            with diagnostics.stage('model_transport_attempt', attempt=attempt, controlled=supported,
                                   connect_timeout_seconds=policy.connect if supported else None,
                                   read_timeout_seconds=policy.read if supported else None,
                                   sdk_max_attempts=1 if supported else None):
                # max_retries is consumed by the common SDK forward signature.
                # The application records each reconnect instead of hiding five
                # long requests inside a single code-generation attempt.
                controls = {'max_retries': 1} if supported else {}
                return client(prompt, **options, **controls)
        except Exception as exc:
            if not supported or attempt == attempts or not retry_connection(exc):
                raise
            time.sleep(1)
