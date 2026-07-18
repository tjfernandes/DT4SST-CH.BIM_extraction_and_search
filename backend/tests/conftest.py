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


class SocketBlockedError(RuntimeError):
    pass


class _GuardedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise SocketBlockedError("Os testes não podem abrir sockets (rede proibida).")


@pytest.fixture(autouse=True)
def isolated_opensearch_env(monkeypatch, tmp_path):
    """Ambiente determinístico por teste.

    Remove todas as variáveis OpenSearch (canónicas e legadas), muda o CWD para
    um diretório vazio — assim o env_file relativo "backend/.env" nunca resolve
    para o ficheiro real — e neutraliza o load_dotenv() usado em reloads de
    shared.config, para que nenhum teste leia o backend/.env verdadeiro.
    """
    for name in OPENSEARCH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Guarda de rede: qualquer tentativa de criar um socket falha o teste."""
    monkeypatch.setattr(socket, "socket", _GuardedSocket)


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
