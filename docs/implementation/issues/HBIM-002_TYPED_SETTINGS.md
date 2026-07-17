# HBIM-002 — Typed OpenSearch settings and client normalization

> Ficheiro-alvo no repositório: `docs/implementation/issues/HBIM-002_TYPED_SETTINGS.md`
> Precedência: esta especificação de issue tem prioridade sobre ROADMAP e HBIM_RAG_DECISIONS quando houver conflito material (ver CLAUDE.md). Não resolver conflitos materiais em silêncio.
> Pré-requisito: **HBIM-001** (rotação da password exposta e remoção do default hardcoded) tem de estar concluído. Se a password antiga ainda estiver presente no código, parar com `BLOCKED — SECRET OR SECURITY RISK`.

---

## Contexto

A configuração de OpenSearch é hoje lida com `os.getenv` disperso, com defaults inseguros, sem tipagem e sem normalização de host/scheme. Além disso, clientes de rede são criados no import de módulos, o que impede import-safety e obriga o extractor IFC a arrastar configuração que não usa. Esta issue introduz definições tipadas (`pydantic-settings`), um cliente OpenSearch normalizado e construído de forma lazy, e o bootstrap mínimo de testes para o validar. É a base segura sobre a qual assentam as milestones seguintes; **não** implementa auth, CI, lint, type-check, testcontainers nem alterações de índices/mappings.

## Objetivo

Substituir a leitura ad-hoc de env por um modelo `OpenSearchSettings` tipado e validado apenas pelos consumidores que o usam, com:
- nomes canónicos + aliases transitórios;
- `verify_certs` seguro por default;
- inferência de SSL apenas a partir do scheme;
- normalização correta de host/scheme/porta;
- construção do cliente lazy, com timeout/retries configuráveis;
- garantia de que imports não criam clientes e de que o extractor IFC importa sem configuração OpenSearch;
- bootstrap mínimo de pytest + `test_config` e testes de import-safety.

## Âmbito

- `OpenSearchSettings` em `pydantic-settings` com `SecretStr` para a password.
- Normalização de host/scheme/porta e derivação de SSL.
- Factory lazy do cliente OpenSearch com `timeout`, `max_retries`, `retry_on_timeout`.
- Remoção da criação de clientes de rede a nível de módulo (OpenSearch **e** o cliente LLM já existente) em `api/search.py`, substituindo por acesso lazy. O redesign da configuração LLM em si fica fora do âmbito — apenas se envolve o cliente num getter lazy.
- `backend/.env.example` com nomes canónicos, valores fictícios e secrets vazios.
- Bootstrap mínimo de pytest (`pytest.ini`, `conftest.py`) e testes `test_config` + import-safety.
- Adição justificada da dependência `pydantic-settings` ao ambiente do projeto (não global).

## Fora do âmbito

- Auth, CORS, healthchecks, logging estruturado (HBIM-003A) e integração de auth no frontend (HBIM-003B).
- CI, `ruff`, `mypy`, testcontainers, `docker-compose` (HBIM-004).
- Redesign das definições de LLM/embedding, tipagem completa dessas settings.
- Índices, mappings, aliases, migração, reindex (HBIM-020+).
- Serviço de embeddings e substituição de modelo (HBIM-030+).
- Qualquer alteração de comportamento de retrieval.

## Estado atual observado

- `backend/shared/config.py`: `load_dotenv()` a nível de módulo e constantes via `os.getenv`. Relevantes: `OPENSEARCH_HOST` (default `localhost`), `OPENSEARCH_PORT` (default `9200`), `OPENSEARCH_USER` (default `admin`), `OPENSEARCH_PASSWORD` (default hardcoded — removido em HBIM-001), `USE_SSL`/`VERIFY_CERTS`/`SSL_SHOW_WARN` (helper `_to_bool`, todos default `False`), `OPENSEARCH_INDEX` (default `bim_elements`). Também define `LLM_*` e `EMBEDDING_*`.
- `backend/shared/opensearch.py`: `get_opensearch_client()` sem argumentos, lê os globais de `config` e devolve `OpenSearch(hosts=[{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}], http_auth=(OPENSEARCH_USER, OPENSEARCH_PASSWORD), use_ssl=USE_SSL, verify_certs=VERIFY_CERTS, ssl_show_warn=SSL_SHOW_WARN)`. **Sem** `timeout`, `max_retries`, `retry_on_timeout`. É factory, mas sem normalização (partiria com host que inclua `http(s)://`).
- `backend/api/search.py`: cria clientes **no import** — `opensearch_client = get_opensearch_client()` e `llm_client = OpenAI(...)` a nível de módulo. Usados em `execute_search`, `execute_aggregation`, `fetch_by_id` (OpenSearch) e `get_response` (LLM).
- `backend/ingestion/index_to_opensearch.py`: importa `OPENSEARCH_INDEX` de `config` e `get_opensearch_client` de `opensearch`; **chama** o factory dentro de `index_data` (não no import).
- `backend/ingestion/extract_bim.py`: **não** importa `shared.config` nem `shared.opensearch`. Já é importável sem configuração OpenSearch; esta issue bloqueia a regressão com um teste.

## Ficheiros existentes a modificar

- `backend/shared/config.py` — introduzir `OpenSearchSettings`; manter as constantes LLM/embedding existentes inalteradas (fora do âmbito). Não deixar a validação de OpenSearch acontecer no import.
- `backend/shared/opensearch.py` — factory passa a receber (opcionalmente) `OpenSearchSettings`, usar os valores normalizados e acrescentar `timeout`/`max_retries`/`retry_on_timeout`.
- `backend/api/search.py` — remover a criação de clientes a nível de módulo; introduzir getters lazy; substituir usos por esses getters.
- `backend/.env.example` — passar a nomes canónicos, valores fictícios, secrets vazios.

## Ficheiros novos

- `backend/tests/__init__.py` (se necessário).
- `backend/tests/conftest.py` — fixtures: limpeza de env OpenSearch; guarda de rede (bloqueio de sockets); patch de construtores de cliente.
- `backend/tests/test_config.py` — testes unitários de settings/normalização.
- `backend/tests/test_import_safety.py` — testes de import-safety.
- `backend/pytest.ini` — bootstrap mínimo do pytest (rootdir/testpaths). **Sem** configuração de CI/coverage/plugins (isso é HBIM-004).

## Classes e modelos

`OpenSearchSettings` (pydantic-settings v2), campos com nomes canónicos e aliases transitórios; `password: SecretStr` obrigatório (sem default); propriedades computadas para host/scheme/porta/SSL efetivos.

## Assinaturas concretas

```python
# backend/shared/config.py
from typing import Literal
from pydantic import AliasChoices, Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class OpenSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="backend/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    host: str = Field(default="localhost",
                      validation_alias=AliasChoices("OPENSEARCH_HOST"))
    port: int = Field(default=9200,
                      validation_alias=AliasChoices("OPENSEARCH_PORT"))
    scheme: Literal["http", "https"] | None = Field(
        default=None, validation_alias=AliasChoices("OPENSEARCH_SCHEME"))
    username: str = Field(default="admin",
                          validation_alias=AliasChoices("OPENSEARCH_USERNAME", "OPENSEARCH_USER"))
    password: SecretStr = Field(
        validation_alias=AliasChoices("OPENSEARCH_PASSWORD"))  # obrigatório, sem default
    use_ssl: bool | None = Field(
        default=None, validation_alias=AliasChoices("OPENSEARCH_USE_SSL", "USE_SSL"))
    verify_certs: bool = Field(
        default=True, validation_alias=AliasChoices("OPENSEARCH_VERIFY_CERTS", "VERIFY_CERTS"))
    ssl_show_warn: bool = Field(
        default=False, validation_alias=AliasChoices("OPENSEARCH_SSL_SHOW_WARN", "SSL_SHOW_WARN"))
    timeout: int = Field(default=30,
                         validation_alias=AliasChoices("OPENSEARCH_TIMEOUT"))
    max_retries: int = Field(default=3,
                             validation_alias=AliasChoices("OPENSEARCH_MAX_RETRIES"))
    retry_on_timeout: bool = Field(
        default=True, validation_alias=AliasChoices("OPENSEARCH_RETRY_ON_TIMEOUT"))

    @model_validator(mode="after")
    def _normalize(self) -> "OpenSearchSettings": ...

    @computed_field
    @property
    def effective_scheme(self) -> Literal["http", "https"]: ...

    @computed_field
    @property
    def effective_host(self) -> str: ...   # apenas hostname, sem scheme nem porta

    @computed_field
    @property
    def effective_port(self) -> int: ...

    @computed_field
    @property
    def effective_use_ssl(self) -> bool:
        # SSL só a partir do scheme; nunca a partir de credenciais
        return self.use_ssl if self.use_ssl is not None else (self.effective_scheme == "https")
```

```python
# backend/shared/opensearch.py
from opensearchpy import OpenSearch
from shared.config import OpenSearchSettings

def build_opensearch_client(settings: OpenSearchSettings) -> OpenSearch: ...

def get_opensearch_client(settings: OpenSearchSettings | None = None) -> OpenSearch:
    # constrói settings (e valida) apenas quando chamado
    return build_opensearch_client(settings or OpenSearchSettings())
```

```python
# backend/api/search.py  (getters lazy; sem instâncias a nível de módulo)
from functools import lru_cache
from opensearchpy import OpenSearch
from openai import OpenAI

@lru_cache(maxsize=1)
def get_search_client() -> OpenSearch: ...   # usa get_opensearch_client()

@lru_cache(maxsize=1)
def get_llm_client() -> OpenAI: ...          # config LLM existente, apenas embrulhada
```

## Estratégia de aliases

- **Nomes canónicos**: `OPENSEARCH_HOST`, `OPENSEARCH_PORT`, `OPENSEARCH_SCHEME`, `OPENSEARCH_USERNAME`, `OPENSEARCH_PASSWORD`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS`, `OPENSEARCH_SSL_SHOW_WARN`, `OPENSEARCH_TIMEOUT`, `OPENSEARCH_MAX_RETRIES`, `OPENSEARCH_RETRY_ON_TIMEOUT`.
- **Aliases legados aceites transitoriamente**: `OPENSEARCH_USER` → `username`; `USE_SSL` → `use_ssl`; `VERIFY_CERTS` → `verify_certs`; `SSL_SHOW_WARN` → `ssl_show_warn`.
- Implementados via `AliasChoices` (o nome canónico **primeiro**, o legado a seguir). Matching case-insensitive (`case_sensitive=False`).
- Se ambos (canónico e legado) estiverem definidos para o mesmo campo, o canónico prevalece; emitir `DeprecationWarning` a indicar o nome legado detetado.
- `.env.example` só documenta os nomes **canónicos**.

## Normalização de URL

Objetivo: aceitar `host` puro, `http://host`, `https://host`, com ou sem porta, e um `OPENSEARCH_SCHEME`/`OPENSEARCH_PORT` explícitos, produzindo `effective_host` (só hostname), `effective_scheme` e `effective_port` coerentes. **Nunca** passar uma URL completa no campo `host` do dicionário de configuração do cliente.

Regras determinísticas (em `_normalize`, usando `urllib.parse.urlsplit` e `self.model_fields_set`):

1. Se `host` contém `://`, extrair `embedded_scheme`, `embedded_hostname`, `embedded_port`. Caso contrário, tentar extrair `embedded_port` de um sufixo `:<porta>` e usar o resto como hostname.
2. **Scheme efetivo**: se `OPENSEARCH_SCHEME` foi fornecido (`"scheme" in model_fields_set`) e existe `embedded_scheme` diferente → erro de configuração (conflito). Se `OPENSEARCH_SCHEME` fornecido → usa-o. Senão, se existe `embedded_scheme` → usa-o. Senão → default `"https"`.
3. **Porta efetiva**: se `OPENSEARCH_PORT` foi fornecido e existe `embedded_port` diferente → erro de configuração (conflito). Senão, `embedded_port` se existir, senão `OPENSEARCH_PORT` (default `9200`).
4. **Host efetivo**: apenas `embedded_hostname` (sem scheme, sem porta).
5. Guardar os valores normalizados para as propriedades computadas os exporem.

Casos de referência: `os.example.test` → `(host=os.example.test, scheme=https[default], port=9200)`; `http://os.example.test` → `(scheme=http)`; `https://os.example.test:9243` → `(scheme=https, port=9243)`; `os.example.test:9200` + `OPENSEARCH_SCHEME=https` → `(host=os.example.test, scheme=https, port=9200)`.

## Ciclo de vida do cliente

- Nenhum cliente é criado durante import. `get_opensearch_client()`/`get_search_client()`/`get_llm_client()` só instanciam quando chamados.
- `OpenSearchSettings()` é construído e **validado no momento em que o cliente é criado/usado**, não no import de `config`.
- O cliente OpenSearch é construído com:
  - `hosts=[{"host": settings.effective_host, "port": settings.effective_port}]` (hostname puro — decisão 9);
  - `use_ssl=settings.effective_use_ssl`;
  - `verify_certs=settings.verify_certs`;
  - `ssl_show_warn=settings.ssl_show_warn`;
  - `http_auth=(settings.username, settings.password.get_secret_value())` (única chamada a `get_secret_value`, dentro da factory);
  - `timeout=settings.timeout`, `max_retries=settings.max_retries`, `retry_on_timeout=settings.retry_on_timeout`.
- Em `api/search.py`, `get_search_client()`/`get_llm_client()` são singletons lazy (`lru_cache`); todos os usos passam a chamá-los.

## Política de secrets

- `password` é `SecretStr`; nunca aparece em `repr`, `str`, mensagens de erro ou logs.
- `get_secret_value()` só é invocado dentro de `build_opensearch_client`, nunca em logging.
- Não logar objetos `OpenSearchSettings` completos que revelem o secret (o `SecretStr` já mascara, mas não contornar).
- O agente **nunca** abre, lê ou altera `backend/.env`.
- `backend/.env.example` contém apenas nomes canónicos, valores fictícios (`.example.test`) e `OPENSEARCH_PASSWORD=` vazio.
- Nenhum host, username, password, token ou API key reais em código, testes, documentação ou logs.

## Tratamento de erros

- **Password em falta**: instanciar `OpenSearchSettings()` sem `OPENSEARCH_PASSWORD` levanta `ValidationError` com mensagem clara (indicar que `OPENSEARCH_PASSWORD` é obrigatório) — a mensagem **não** contém valores de secret.
- **Conflitos de normalização** (scheme explícito ≠ scheme embutido no host; porta explícita ≠ porta embutida): levantar erro de configuração claro identificando o conflito; não resolver silenciosamente.
- **Scheme inválido**: valores fora de `{"http","https"}` rejeitados pela tipagem `Literal`.
- Erros de settings propagam para o chamador (consumidor OpenSearch), não para o import.

## Compatibilidade transitória

- Aceitar os aliases legados durante esta fase; emitir `DeprecationWarning` quando detetados.
- Manter `get_opensearch_client()` chamável sem argumentos (constrói settings internamente), para não partir `index_to_opensearch.py`.
- Não remover os nomes legados nesta issue; a remoção é uma limpeza futura documentada.
- Não alterar a configuração LLM/embedding para além de embrulhar o cliente LLM num getter lazy.

## Ordem file-by-file da implementação

1. Adicionar `pydantic-settings` ao ficheiro de dependências do projeto (ambiente do projeto, não global); confirmar pydantic v2.
2. `backend/shared/config.py`: adicionar `OpenSearchSettings` (campos, aliases, `_normalize`, propriedades computadas). Não introduzir validação de OpenSearch no import.
3. `backend/shared/opensearch.py`: `build_opensearch_client(settings)` + `get_opensearch_client(settings=None)` com normalização, auth e timeout/retries.
4. `backend/api/search.py`: remover instâncias a nível de módulo; adicionar `get_search_client()`/`get_llm_client()` lazy; atualizar todos os usos (`execute_search`, `execute_aggregation`, `fetch_by_id`, `get_response`).
5. `backend/.env.example`: nomes canónicos, valores fictícios, secrets vazios.
6. `backend/pytest.ini` + `backend/tests/conftest.py` (fixtures de env e guarda de rede).
7. `backend/tests/test_config.py` e `backend/tests/test_import_safety.py`.
8. Correr os testes localmente (offline) e iterar.

## Testes unitários (`backend/tests/test_config.py`)

Todos com valores sintéticos e domínios `.example.test`, via `monkeypatch.setenv`/`delenv`. Sem rede.

- `test_host_bare` — `OPENSEARCH_HOST=os.example.test` ⇒ `effective_host=="os.example.test"`, `effective_scheme=="https"`, `effective_port==9200`.
- `test_host_http_scheme` — `http://os.example.test` ⇒ `effective_scheme=="http"`, `effective_host=="os.example.test"`.
- `test_host_https_scheme` — `https://os.example.test` ⇒ `effective_scheme=="https"`.
- `test_explicit_port` — `https://os.example.test:9243` ⇒ `effective_port==9243`; e `OPENSEARCH_PORT=9243` explícito com host puro ⇒ `effective_port==9243`.
- `test_ssl_explicit` — `OPENSEARCH_USE_SSL=true` ⇒ `effective_use_ssl is True`; `false` ⇒ `is False` (independente do scheme).
- `test_ssl_derived_from_scheme` — sem `OPENSEARCH_USE_SSL`: `https://...` ⇒ `True`; `http://...` ⇒ `False`.
- `test_http_with_credentials_does_not_enable_ssl` — `http://os.example.test` + username/password definidos, sem `OPENSEARCH_USE_SSL` ⇒ `effective_use_ssl is False`.
- `test_verify_certs_default_true` — sem env ⇒ `verify_certs is True`.
- `test_verify_certs_explicit_false` — `OPENSEARCH_VERIFY_CERTS=false` ⇒ `verify_certs is False`.
- `test_legacy_aliases` — definir `OPENSEARCH_USER`, `USE_SSL`, `VERIFY_CERTS`, `SSL_SHOW_WARN` ⇒ mapeados para `username`, `use_ssl`, `verify_certs`, `ssl_show_warn`; e `DeprecationWarning` emitido.
- `test_secret_not_exposed` — `repr(settings)`, `str(settings)` e a mensagem de qualquer `ValidationError` **não** contêm o valor da password.
- `test_missing_password_raises` — sem `OPENSEARCH_PASSWORD` ⇒ `ValidationError` clara; a mensagem menciona `OPENSEARCH_PASSWORD` e não contém secrets.
- `test_scheme_conflict_raises` — `OPENSEARCH_HOST=http://os.example.test` + `OPENSEARCH_SCHEME=https` ⇒ erro de configuração.

## Testes de import safety (`backend/tests/test_import_safety.py`)

- `test_import_search_creates_no_client` — com patch dos construtores (`shared.opensearch.build_opensearch_client` e `openai.OpenAI`) a registar chamadas, `importlib.reload(api.search)`; assert `call_count == 0`.
- `test_extractor_imports_without_opensearch_env` — `monkeypatch.delenv` de todos os `OPENSEARCH_*` (e legados); `importlib.import_module("ingestion.extract_bim")` sem erro; a função de extração é chamável sem qualquer configuração OpenSearch.
- `test_no_network_on_import` — fixture que substitui `socket.socket` por um stub que levanta se instanciado; importar/`reload` de `shared.config`, `shared.opensearch`, `api.search`, `ingestion.extract_bim` sem qualquer tentativa de socket.
- `test_config_import_does_not_validate_opensearch` — importar `shared.config` sem `OPENSEARCH_PASSWORD` **não** levanta (a validação só ocorre ao instanciar `OpenSearchSettings()`).

Fixtures em `conftest.py`: limpeza determinística de env (`autouse` a remover `OPENSEARCH_*`/legados antes de cada teste), guarda de rede (patch de `socket.socket`), e patch dos construtores de cliente.

## Comandos de validação

Executar a partir da raiz do repositório (WSL, filesystem Linux), no ambiente Python do projeto:

- `pytest backend/tests/test_config.py backend/tests/test_import_safety.py -q` — todos verdes, offline.
- `python -c "import ingestion.extract_bim"` com os `OPENSEARCH_*` por definir — sem erro.
- `git ls-files backend/.env` — saída vazia (não tracked).
- Verificar `backend/.env.example` — só nomes canónicos, valores `.example.test`, `OPENSEARCH_PASSWORD=` vazio.
- `git diff --check`
- `git diff --stat`
- Rever o diff para confirmar ausência de secrets, hosts e usernames reais.

(CI, `ruff`, `mypy`, testcontainers e compose **não** fazem parte desta issue — HBIM-004.)

## Critérios de aceitação

Cada critério é `PASS`/`FAIL`/`PARTIAL` com evidência (ficheiro, símbolo, teste).

1. **Host puro** normaliza para hostname sem scheme/porta — `test_host_bare`.
2. **Host com `http://`** ⇒ `effective_scheme=="http"` — `test_host_http_scheme`.
3. **Host com `https://`** ⇒ `effective_scheme=="https"` — `test_host_https_scheme`.
4. **Porta explícita** respeitada (embutida ou via `OPENSEARCH_PORT`) — `test_explicit_port`.
5. **SSL explícito** (`OPENSEARCH_USE_SSL`) respeitado — `test_ssl_explicit`.
6. **SSL derivado do scheme** quando `OPENSEARCH_USE_SSL` ausente — `test_ssl_derived_from_scheme`.
7. **Credenciais com HTTP não ativam SSL** — `test_http_with_credentials_does_not_enable_ssl`.
8. **`verify_certs` true por default** — `test_verify_certs_default_true`.
9. **`verify_certs` false explícito** respeitado — `test_verify_certs_explicit_false`.
10. **Aliases legados** aceites com `DeprecationWarning` — `test_legacy_aliases`.
11. **`SecretStr` não aparece** em `repr`, erros ou logs — `test_secret_not_exposed`.
12. **Consumidor OpenSearch falha claramente sem password** — `test_missing_password_raises`.
13. **Extractor IFC importa/corre sem password** e sem env OpenSearch — `test_extractor_imports_without_opensearch_env`.
14. **Imports não criam clientes** — `test_import_search_creates_no_client`.
15. **Testes não fazem contactos de rede** — `test_no_network_on_import` (guarda de sockets ativa).
16. Cliente OpenSearch construído com `timeout`, `max_retries`, `retry_on_timeout` e `hosts` com hostname puro — revisão de `build_opensearch_client` + inspeção do objeto em teste (sem rede).
17. `backend/.env.example` só com valores fictícios e secrets vazios; `backend/.env` não tracked.

## Condições que obrigam a parar e pedir decisão humana

Usar os tokens de bloqueio do CLAUDE.md:

- `BLOCKED — SECRET OR SECURITY RISK` — se a password antiga ainda existir no repositório (HBIM-001 não concluído), se for necessário ler `backend/.env`, ou se algum secret real surgir no diff.
- `BLOCKED — ENVIRONMENT OR DEPENDENCY ISSUE` — se o projeto usar pydantic v1 (incompatível com estas assinaturas v2) ou se não for possível adicionar `pydantic-settings` ao ambiente do projeto.
- `BLOCKED — UNEXPECTED REPOSITORY STATE` — se `backend/shared/config.py`, `backend/shared/opensearch.py`, `backend/api/search.py` ou `backend/ingestion/extract_bim.py` divergirem materialmente do "estado atual observado".
- `BLOCKED — ARCHITECTURAL DECISION REQUIRED` — se alterar a assinatura de `get_opensearch_client()` implicar mudar consumidores fora do âmbito, se for pedida a remoção imediata dos aliases legados, ou se a resolução de conflitos scheme/porta exigir uma política não coberta acima.
- `BLOCKED — SPECIFICATION INCOMPLETE` — se a implementação exigir tocar em configuração LLM/embedding, índices, mappings ou aliases para além do embrulho lazy do cliente LLM.