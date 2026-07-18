import importlib

import pytest

import api.errors
import api.health
import api.main
import api.metrics
import api.middleware
import api.search
import ingestion.extract_bim
import shared.config
import shared.logging
import shared.opensearch
import shared.security

# Ordem de dependência: config antes dos módulos que o consomem; api.main no
# fim (importa todos os restantes), para que os reloads deixem o grafo de
# módulos internamente consistente.
_PRODUCTION_MODULES_IN_DEPENDENCY_ORDER = (
    shared.config,
    shared.security,
    shared.logging,
    shared.opensearch,
    api.errors,
    api.middleware,
    api.metrics,
    api.health,
    ingestion.extract_bim,
    api.search,
    api.main,
)


@pytest.fixture(autouse=True)
def restore_production_modules():
    """Recarrega os módulos de produção após cada teste deste ficheiro.

    Os testes de import-safety fazem importlib.reload; sem esta reposição,
    testes posteriores observariam gerações de classes e bindings divergentes
    (suite dependente da ordem).
    """
    yield
    for module in _PRODUCTION_MODULES_IN_DEPENDENCY_ORDER:
        importlib.reload(module)


def test_import_search_creates_no_client(client_constructor_recorder):
    importlib.reload(api.search)
    importlib.reload(api.main)
    assert client_constructor_recorder["opensearch"] == 0
    assert client_constructor_recorder["openai"] == 0


def test_extractor_imports_without_opensearch_env():
    # A fixture autouse já removeu todas as variáveis OPENSEARCH_* e legadas.
    module = importlib.reload(ingestion.extract_bim)
    assert callable(module.extract_bim_data)


def test_no_network_on_import():
    # A guarda de sockets autouse falha o teste se algum import tentar rede.
    for module in _PRODUCTION_MODULES_IN_DEPENDENCY_ORDER:
        importlib.reload(module)


def test_config_import_does_not_validate_opensearch():
    # Sem OPENSEARCH_PASSWORD definido, importar shared.config não levanta;
    # a validação só ocorre ao instanciar OpenSearchSettings().
    module = importlib.reload(shared.config)
    assert hasattr(module, "OpenSearchSettings")
    assert hasattr(module, "ApiSettings")


def test_api_main_importable_without_api_env():
    # Sem API_* definidos, importar/reconstruir api.main não levanta: a app
    # constrói com defaults (auth ativa, sem chaves) e o fail-closed dispara
    # apenas no primeiro uso ou no arranque (lifespan), nunca no import.
    module = importlib.reload(api.main)
    assert hasattr(module, "app")
    assert callable(module.create_app)
