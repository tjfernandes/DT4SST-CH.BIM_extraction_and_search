import importlib
import socket
from pathlib import Path

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


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _assert_loopback_destination(sock: socket.socket, address: object) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    host: object = address[0] if isinstance(address, tuple) and address else address
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host not in _LOOPBACK_HOSTS:
        raise SocketBlockedError(
            f"Testes de integração só podem contactar loopback; destino bloqueado: {host!r}"
        )


class _LoopbackOnlySocket(socket.socket):
    """Sockets de rede permitidos apenas para destinos loopback (integração)."""

    def connect(self, address):
        _assert_loopback_destination(self, address)
        return super().connect(address)

    def connect_ex(self, address):
        _assert_loopback_destination(self, address)
        return super().connect_ex(address)


@pytest.fixture(autouse=True)
def isolated_opensearch_env(monkeypatch, tmp_path):
    """Ambiente determinístico por teste.

    Remove todas as variáveis OpenSearch/API (canónicas e legadas) e muda o
    CWD para um diretório vazio — assim o env_file relativo "backend/.env"
    nunca resolve para o ficheiro real. O isolamento dotenv adicional vive na
    fixture forbid_real_env_files.
    """
    for name in OPENSEARCH_ENV_VARS + API_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def network_guard(request, monkeypatch):
    """Guarda de rede consciente de markers — nunca desativada.

    unit (default, incl. testes sem marker): criação de sockets de rede
    proibida. integration: sockets de rede permitidos apenas para destinos
    loopback; qualquer outro destino falha o teste.
    """
    if "integration" in request.keywords:
        monkeypatch.setattr(socket, "socket", _LoopbackOnlySocket)
    else:
        monkeypatch.setattr(socket, "socket", _GuardedSocket)


@pytest.fixture(autouse=True)
def forbid_real_env_files(monkeypatch):
    """Nenhum teste pode carregar os .env reais.

    O chdir para tmp_path já impede a resolução relativa; esta fixture
    acrescenta a asserção exigida pela spec: qualquer chamada dotenv dirigida
    a backend/.env ou frontend/.env reais falha o teste, e as leituras dotenv
    em testes devolvem sempre vazio (reloads de shared.config incluídos).
    """
    import dotenv
    import dotenv.main as dotenv_main

    repo_root = Path(__file__).resolve().parents[2]
    forbidden = {repo_root / "backend" / ".env", repo_root / "frontend" / ".env"}

    def _check(dotenv_path: object) -> None:
        if not dotenv_path:
            return
        try:
            resolved = Path(str(dotenv_path)).resolve()
        except (OSError, ValueError):
            return
        if resolved in forbidden:
            pytest.fail(f"Teste tentou carregar um .env real: {resolved}")

    def guarded_load_dotenv(dotenv_path=None, *args, **kwargs):
        _check(dotenv_path)
        return False

    def guarded_dotenv_values(dotenv_path=None, *args, **kwargs):
        _check(dotenv_path)
        return {}

    monkeypatch.setattr(dotenv, "load_dotenv", guarded_load_dotenv)
    monkeypatch.setattr(dotenv_main, "load_dotenv", guarded_load_dotenv)
    monkeypatch.setattr(dotenv, "dotenv_values", guarded_dotenv_values)
    monkeypatch.setattr(dotenv_main, "dotenv_values", guarded_dotenv_values)


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

    # HBIM-040: o routing deixou de ser feito por LLM, por isso o /chat já não
    # emite a chamada CLASSIFY_INTENT que consumia a primeira resposta desta
    # lista. No caminho "chat" a primeira (e única) chamada ao LLM é a resposta
    # final ao utilizador.
    responses = ["resposta final"]
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
