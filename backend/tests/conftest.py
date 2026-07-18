import importlib
import socket

import pytest

OPENSEARCH_ENV_VARS = (
    "OPENSEARCH_HOST",
    "OPENSEARCH_PORT",
    "OPENSEARCH_SCHEME",
    "OPENSEARCH_USERNAME",
    "OPENSEARCH_USER",
    "OPENSEARCH_PASSWORD",
    "OPENSEARCH_USE_SSL",
    "USE_SSL",
    "OPENSEARCH_VERIFY_CERTS",
    "VERIFY_CERTS",
    "OPENSEARCH_SSL_SHOW_WARN",
    "SSL_SHOW_WARN",
    "OPENSEARCH_TIMEOUT",
    "OPENSEARCH_MAX_RETRIES",
    "OPENSEARCH_RETRY_ON_TIMEOUT",
    "OPENSEARCH_INDEX",
)

API_ENV_VARS = (
    "API_AUTH_ENABLED",
    "API_KEYS",
    "METRICS_PUBLIC",
    "CORS_ALLOW_ORIGINS",
    "CORS_ALLOW_CREDENTIALS",
    "LOG_FORMAT",
)

SYNTHETIC_API_KEY = "synthetic-key-0123456789abcdef"


class SocketBlockedError(RuntimeError):
    pass


class _GuardedSocket(socket.socket):
    """Bloqueia sockets de REDE (AF_INET/AF_INET6, incluindo o default).

    AF_UNIX continua permitido: o event loop do asyncio (usado pelo TestClient,
    sempre in-process) cria um self-pipe local via socketpair(), sem rede.
    """

    def __init__(self, family: int = -1, *args, **kwargs):
        if family == -1 or family in (socket.AF_INET, socket.AF_INET6):
            raise SocketBlockedError(
                "Os testes não podem abrir sockets de rede (AF_INET/AF_INET6)."
            )
        super().__init__(family, *args, **kwargs)


@pytest.fixture(autouse=True)
def isolated_opensearch_env(monkeypatch, tmp_path):
    """Ambiente determinístico por teste.

    Remove todas as variáveis OpenSearch (canónicas e legadas), muda o CWD para
    um diretório vazio — assim o env_file relativo "backend/.env" nunca resolve
    para o ficheiro real — e neutraliza o load_dotenv() usado em reloads de
    shared.config, para que nenhum teste leia o backend/.env verdadeiro.
    """
    for name in OPENSEARCH_ENV_VARS + API_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Guarda de rede: qualquer tentativa de criar um socket falha o teste."""
    monkeypatch.setattr(socket, "socket", _GuardedSocket)


@pytest.fixture(autouse=True)
def reset_api_state():
    """Repõe estado partilhado da API entre testes: cache de settings e
    handlers de logging da aplicação (evita duplicação e dependência de ordem)."""
    yield
    import shared.config as config
    import shared.logging as shared_logging

    config.get_api_settings.cache_clear()
    shared_logging.remove_app_handlers()


@pytest.fixture
def make_app():
    """Constrói uma app FastAPI fresca com ApiSettings sintéticos.

    Uma app por teste (registry de métricas e overrides próprios) — os testes
    nunca partilham estado através da app do módulo.
    """
    import api.main as api_main
    import shared.config as config

    def _make(**overrides):
        values = dict(
            auth_enabled=True,
            api_keys=[SYNTHETIC_API_KEY],
            metrics_public=False,
            cors_allow_origins=["http://frontend.example.test"],
            cors_allow_credentials=False,
            log_format="json",
        )
        values.update(overrides)
        settings = config.ApiSettings(_env_file=None, **values)
        return api_main.create_app(settings)

    return _make


class _FakeLlmMessage:
    def __init__(self, content: str):
        self.content = content


@pytest.fixture
def fake_llm(monkeypatch):
    """Guia o /chat pela rota 'chat' com respostas determinísticas, sem LLM."""
    import api.main as api_main

    responses = ['{"search_strategy": "chat"}', "resposta final"]
    state = {"index": 0}

    def _fake_get_response(prompt, history=None, response_format=None):
        content = responses[min(state["index"], len(responses) - 1)]
        state["index"] += 1
        return _FakeLlmMessage(content)

    monkeypatch.setattr(api_main, "get_response", _fake_get_response)
    return state


@pytest.fixture
def client_constructor_recorder():
    """Substitui os construtores de cliente por gravadores de chamadas.

    Usa um MonkeyPatch próprio para controlar a ordem do teardown: primeiro
    desfaz os patches, depois recarrega api.search — assim nenhum binding
    fake (OpenAI/OpenSearch) sobrevive no namespace do módulo após o teste,
    independentemente da ordem de execução da suite.
    """
    import openai

    import api.search
    import shared.opensearch as shared_opensearch

    calls = {"opensearch": 0, "openai": 0}

    def _fake_build_opensearch_client(settings=None):
        calls["opensearch"] += 1
        return object()

    class _FakeOpenAI:
        def __init__(self, *args, **kwargs):
            calls["openai"] += 1

    patcher = pytest.MonkeyPatch()
    try:
        patcher.setattr(
            shared_opensearch, "build_opensearch_client", _fake_build_opensearch_client
        )
        patcher.setattr(openai, "OpenAI", _FakeOpenAI)
        yield calls
    finally:
        patcher.undo()
        importlib.reload(api.search)
