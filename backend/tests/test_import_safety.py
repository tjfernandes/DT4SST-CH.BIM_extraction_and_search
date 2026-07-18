import importlib

import pytest

import api.search
import ingestion.extract_bim
import shared.config
import shared.opensearch

# Ordem de dependência: config antes de opensearch/api.search, para que os
# reloads deixem o grafo de módulos internamente consistente.
_PRODUCTION_MODULES_IN_DEPENDENCY_ORDER = (
    shared.config,
    shared.opensearch,
    ingestion.extract_bim,
    api.search,
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
