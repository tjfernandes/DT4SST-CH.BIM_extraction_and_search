# Local development setup (HBIM-004)

Ambiente de referência: **WSL2 (filesystem Linux)**, conda env **`hbim-rag`**
(Python 3.10). Nota de reconciliação: o README histórico referia um env conda
`bim_data` criado a partir de `backend/environment.yml`; esse ficheiro já não
existe e o ambiente operativo é `hbim-rag`. Todos os comandos Python correm
via `conda run -n hbim-rag`.

## Dependências

```bash
# Runtime (não-ML) + tooling de desenvolvimento — suficiente para toda a suite
~/miniconda3/bin/conda run -n hbim-rag python -m pip install \
  -r backend/requirements.txt -r backend/requirements-dev.txt

# Stack ML (embeddings; multi-GB) — apenas para indexação/rota semântica
~/miniconda3/bin/conda run -n hbim-rag python -m pip install \
  -r backend/requirements-ml.txt
```

Os jobs de CI unit, Ruff, mypy e integração **não** instalam
`requirements-ml.txt`.

## Testes

Testes sem marker são tratados como **unitários**. Integração é **opt-in**
(o default `addopts = -m "not integration"` vive no `pyproject.toml`).

```bash
# Unit (inclui os testes sem marker; exclui integração; sem Docker)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -m "not integration"

# Integração (exige Docker local; container OpenSearch efémero)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts="" -m integration

# Suite completa (unmarked + unit + integration)
~/miniconda3/bin/conda run -n hbim-rag python -m pytest backend/tests -q -o addopts=""
```

A ordem dos testes é aleatória por defeito (`pytest-randomly`; a seed é
impressa e reproduzível com `--randomly-seed=N`). A guarda de rede nunca é
desativada: testes unit não podem abrir sockets de rede; testes integration
só podem contactar loopback.

## Qualidade

```bash
~/miniconda3/bin/conda run -n hbim-rag python -m ruff check backend

# Gate bloqueante do mypy (exatamente os oito módulos tipados de HBIM-002/003)
~/miniconda3/bin/conda run -n hbim-rag python -m mypy \
  backend/shared/config.py backend/shared/opensearch.py \
  backend/shared/security.py backend/shared/logging.py \
  backend/api/health.py backend/api/metrics.py \
  backend/api/middleware.py backend/api/errors.py

# Scan informativo (não bloqueante) do resto do backend
~/miniconda3/bin/conda run -n hbim-rag python -m mypy backend
```

## Serviços locais de desenvolvimento (Docker Compose)

Imagens pinadas: `opensearchproject/opensearch:2.19.1` e `neo4j:5.26.0`.
O compose é **apenas para desenvolvimento local** (portas em loopback,
credencial Neo4j sintética `neo4j/localdevpassword`, OpenSearch sem plugin de
segurança). A suite de testes não o usa — a integração cria containers
efémeros via Testcontainers.

```bash
docker compose -f docker-compose.dev.yml config     # validar
docker compose -f docker-compose.dev.yml up -d      # arrancar
docker compose -f docker-compose.dev.yml ps         # estado/health
curl -s http://127.0.0.1:9200/_cluster/health       # OpenSearch
curl -s -I http://127.0.0.1:7474                    # Neo4j
docker compose -f docker-compose.dev.yml down       # parar
docker compose -f docker-compose.dev.yml down -v    # parar e apagar volumes (destrutivo)
```

## Nota WSL + Docker Desktop

O Docker tem de estar acessível **de dentro do WSL** (Docker Desktop →
Settings → Resources → WSL Integration, ativado para esta distro). Os testes
de integração detetam o daemon via SDK Python pela seguinte ordem:
`DOCKER_HOST` explícito → `/var/run/docker.sock` → socket proxy do Docker
Desktop (`/mnt/wsl/docker-desktop/shared-sockets/host-services/docker.proxy.sock`,
se acessível ao utilizador). Se nenhum funcionar, reiniciar o Docker Desktop
(e, se necessário, `wsl --shutdown` no Windows) costuma recriar
`/var/run/docker.sock`. Sem Docker, os testes de integração fazem **skip**
com razão explícita; em CI (`HBIM_REQUIRE_DOCKER=1`) a mesma condição é uma
falha dura.

## CI

Workflow único em `.github/workflows/ci.yml` (push + pull_request), com
`permissions: contents: read`, sem `secrets.*` e sem `.env`: jobs
`backend-unit`, `ruff`, `mypy` (gate dos oito módulos), `frontend`
(`npm ci` + lint + build, Node 22) e `integration-opensearch`
(`needs: backend-unit`, `HBIM_REQUIRE_DOCKER=1`).
