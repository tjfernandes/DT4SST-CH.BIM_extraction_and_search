import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from opensearchpy import OpenSearch
from testcontainers.core.container import DockerContainer

# Imagem pinada; confirmada localmente durante o gate de ambiente (HBIM-004).
OPENSEARCH_IMAGE = "opensearchproject/opensearch:2.19.1"
OPENSEARCH_PORT = 9200
READINESS_TIMEOUT_SECONDS = 120

# Fallback WSL/Docker Desktop: quando /var/run/docker.sock não existe na
# distro, o daemon pode estar exposto no socket proxy partilhado.
_DOCKER_DESKTOP_PROXY_SOCKET = Path(
    "/mnt/wsl/docker-desktop/shared-sockets/host-services/docker.proxy.sock"
)


def _resolve_docker_host() -> None:
    """Aponta o SDK para um endpoint utilizável sem tocar em CI/Linux nativo.

    Ordem: DOCKER_HOST explícito > /var/run/docker.sock > proxy do Docker
    Desktop (apenas se acessível a este utilizador).
    """
    if os.environ.get("DOCKER_HOST"):
        return
    if Path("/var/run/docker.sock").exists():
        return
    proxy = _DOCKER_DESKTOP_PROXY_SOCKET
    if proxy.exists() and os.access(proxy, os.R_OK | os.W_OK):
        os.environ["DOCKER_HOST"] = f"unix://{proxy}"


def _docker_available() -> bool:
    # Deteção via SDK (o mesmo caminho que o Testcontainers usa) — o shim CLI
    # do Docker Desktop em WSL não é fiável a partir de subprocessos.
    _resolve_docker_host()
    try:
        import docker

        client = docker.from_env()
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


def _build_container() -> DockerContainer:
    # Preferir o módulo dedicado do testcontainers; cair para o DockerContainer
    # genérico com o mesmo pin/env se o módulo ou a sua assinatura faltarem.
    try:
        from testcontainers.opensearch import OpenSearchContainer

        container: DockerContainer = OpenSearchContainer(OPENSEARCH_IMAGE)
    except Exception:
        container = DockerContainer(OPENSEARCH_IMAGE)
        container.with_exposed_ports(OPENSEARCH_PORT)
    # Single-node com o plugin de segurança desligado: não existem credenciais.
    container.with_env("discovery.type", "single-node")
    container.with_env("DISABLE_SECURITY_PLUGIN", "true")
    container.with_env("OPENSEARCH_JAVA_OPTS", "-Xms512m -Xmx512m")
    return container


def _wait_until_ready(host: str, port: int) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        client = OpenSearch(
            hosts=[{"host": host, "port": port}], use_ssl=False, timeout=5
        )
        try:
            health = client.cluster.health()
            if health.get("status") in ("yellow", "green"):
                return
        except Exception as exc:  # noqa: B902 — polling de readiness: qualquer erro é "ainda não pronto"
            last_error = exc
        time.sleep(2)
    pytest.fail(
        f"OpenSearch ({OPENSEARCH_IMAGE}) não ficou pronto em "
        f"{READINESS_TIMEOUT_SECONDS}s; último erro: "
        f"{type(last_error).__name__ if last_error else 'sem estado saudável'}"
    )


@pytest.fixture(scope="session")
def opensearch_service() -> Iterator[tuple[str, int]]:
    """Container OpenSearch efémero e LOCAL para os testes de integração.

    Sem Docker disponível: skip com razão explícita; com HBIM_REQUIRE_DOCKER=1
    (CI), a mesma condição é uma falha dura. Teardown garantido pelo context
    manager mesmo em falha (container e volumes removidos).
    """
    if not _docker_available():
        message = "Docker indisponível — pré-requisito dos testes de integração."
        if os.environ.get("HBIM_REQUIRE_DOCKER") == "1":
            pytest.fail(f"HBIM_REQUIRE_DOCKER=1 mas: {message}")
        pytest.skip(message)

    container = _build_container()
    with container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(OPENSEARCH_PORT))
        _wait_until_ready(host, port)
        yield host, port


@pytest.fixture(scope="session")
def opensearch_client(
    opensearch_service: tuple[str, int],
) -> Iterator[OpenSearch]:
    # Cliente construído SEMPRE do host/porta mapeados do container — nunca de
    # OpenSearchSettings nem de qualquer ficheiro .env.
    host, port = opensearch_service
    client = OpenSearch(
        hosts=[{"host": host, "port": port}],
        use_ssl=False,
        verify_certs=False,
        timeout=10,
    )
    yield client
