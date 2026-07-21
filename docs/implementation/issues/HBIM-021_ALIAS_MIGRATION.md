# HBIM-021 — Alias Migration and Non-Destructive Index Lifecycle

> **Tipo:** especificação executável de issue.
> **Branch obrigatória:** `feat/hbim-021-alias-migration`.
> **Depende de:** HBIM-020 (quatro mappings estáticos, merged). Bloqueia HBIM-022.

---

## 0. Precedência e fonte de verdade

1. **Mappings HBIM-020 implementados** (`backend/canonical/mappings/*.json`) — contrato dos índices; **não** são alterados.
2. **Schema canónico v1.0** (`backend/canonical/schema.py`) — `_meta.record_type` deriva daqui; **não** é alterado.
3. Comportamento implementado por HBIM-011/012/020.
4. Esta decisão de review.
5. ROADMAP (mais baixo; a redação de M2/HBIM-021 sobre "indexar" é superada — §2).

**Esta issue só adiciona** um módulo de lifecycle (`index_lifecycle.py`), uma CLI (`migrate.py`), os seus testes, e uma alteração **mínima e não destrutiva** ao `create_index` legacy. **Não** altera os quatro JSON, `backend/canonical`, `shared/config.py`, `shared/opensearch.py`, API/retrieval, `backend/eval` nem a baseline HBIM-005.

---

## 1. Objetivo e fronteiras

Introduzir um **lifecycle seguro e não destrutivo** para os quatro índices da HBIM-020: carregar os mappings, definir aliases lógicos e índices físicos versionados, criar índices **sem apagar** nada, **promover aliases atomicamente**, fazer **rollback explícito**, consultar **status determinístico**, e **neutralizar** a recriação destrutiva do indexer legacy. Testável offline e contra **OpenSearch 2.19.1 efémero** (Testcontainers).

### Pertence à HBIM-021
Loader dos quatro mappings; registry fixo de record types; aliases lógicos; nomes físicos versionados; settings operacionais mínimos; criação **não destrutiva e idempotente**; promoção **atómica** (single-alias e multi-alias); rollback **explícito**; status determinístico; CLI de lifecycle; neutralização do `delete+create` legacy; testes offline + integração; documentação operacional.

### Não pertence à HBIM-021 (HBIM-022 ou posterior)
Leitura de JSONL canónico; projection de `PropertyFact`; bulk indexing; indexers separados; política final de `_id`; embeddings; vetores; chunks; reindex denso; conversão do índice legacy `bim_elements`; retrieval/API a consumir os novos aliases; alteração do schema canónico.

A HBIM-021 gere **índices vazios, lifecycle e aliases**. A **população real** dos índices é HBIM-022.

---

## 2. Conflito com o ROADMAP (resolução com evidência)

- **ROADMAP HBIM-021** (§799–803): "`migrate.py` cria `*_vN`, **indexa**, promove alias; remove `delete+create`." A palavra *indexa* mistura população com lifecycle.
- **ROADMAP HBIM-022** (§805–809): "Indexers separados… **a partir do JSONL canónico**." → leitura de JSONL + bulk indexing são **HBIM-022**.
- **ROADMAP M2** (§321–356) fala em `migrate.py` "ler `bim_elements` e converter via canónico" — essa conversão é **fora de scope** agora.
- **Chunks**: o título de HBIM-020 no ROADMAP nomeia chunks, mas a HBIM-020 implementada **adiou chunks para HBIM-070**. Esta issue mantém **quatro índices, sem chunks**.

**Resolução:** a divisão fina HBIM-020 (mappings) → HBIM-021 (lifecycle/aliases) → HBIM-022 (indexers/JSONL) **supera** a redação grosseira do M2. Sem conflito bloqueante; esta spec fixa a fronteira.

---

## 3. Registry fixo dos quatro record types

Exatamente quatro record types; **sem chunks**. Registry **fixo e fechado** (fonte única do lifecycle):

| record_type | alias lógico | mapping (ficheiro HBIM-020) | `_meta.record_type` esperado |
|---|---|---|---|
| `element` | `hbim_elements` | `elements_v1.json` | `element` |
| `property_fact` | `hbim_property_facts` | `property_facts_v1.json` | `property_fact` |
| `classification_fact` | `hbim_classification_facts` | `classification_facts_v1.json` | `classification_fact` |
| `document` | `hbim_documents` | `documents_v1.json` | `document` |

- **Não reutilizar `bim_elements`** (default legacy de `config.OPENSEARCH_INDEX`): pode existir como **índice concreto** e o OpenSearch **proíbe** um alias com o mesmo nome de um índice concreto.
- O prefixo `hbim_` não colide com `bim_elements`. ✔
- O registry é a **única** fonte de aliases/ficheiros; nenhum nome é derivado de input do utilizador.

---

## 4. Naming físico e as quatro versões

### 4.1 Gramática do nome físico
```
<alias>_v<physical_version>
```
Exemplos: `hbim_elements_v1`, `hbim_property_facts_v1`, `hbim_classification_facts_v1`, `hbim_documents_v1`, e `..._v2`, `..._v3`, …

- A CLI recebe **`--physical-version <inteiro positivo>`** (≥ 1). Nomes físicos arbitrários **não** são aceites: o nome é **composto** pelo lifecycle a partir do registry + versão.
- `InvalidPhysicalVersionError` para: não-inteiro, ≤ 0, ou fora de um limite superior razoável (guardrail; ex.: > 10000).

### 4.2 Quatro versões **distintas** (não confundir)
| Conceito | Onde vive | Exemplo |
|---|---|---|
| **Canonical schema version** | `_meta.canonical_schema_versions` | `["1.0"]` |
| **Mapping (file) version** | `_meta.mapping_version` + ficheiro `_v1.json` | `"1"` |
| **Physical index version** | sufixo `_v<N>` do índice físico (**explícito**) | `hbim_elements_v2` |
| **Logical alias** | registry | `hbim_elements` |

A **physical version nunca é inferida** da versão do ficheiro de mapping (`elements_v1.json` **não** implica `physical_version=1`). São eixos independentes: pode existir `hbim_elements_v3` servido pelo `elements_v1.json`.

---

## 5. Layout de módulos

Dois módulos novos em `backend/ingestion/`:

### 5.1 `index_lifecycle.py` (puro, cliente injetado)
Contém: **registry**, **naming**, **loader**, **settings** (frozen dataclass), **validação**, **lifecycle** (create/promote/rollback/status), **planning** (construtores puros de ações), **exceções**. **Recebe sempre um cliente OpenSearch injetado** (`client: OpenSearch`). **Nunca** cria cliente, settings ou socket no import; **nunca** instancia `OpenSearchSettings`.

### 5.2 `migrate.py` (CLI fina)
Contém apenas: **argparse**, **construção do cliente em runtime** (`get_opensearch_client()` dentro da execução do comando, nunca no import), **output**, **confirmação**, **exit codes**. `main(argv: list[str] | None = None) -> int`; `raise SystemExit(main())`.

**Separação obrigatória:** a lógica testável (planning/validação) vive em `index_lifecycle.py` e é exercitável sem argparse e sem rede; a I/O e os efeitos vivem em `migrate.py`. Padrão idêntico a `eval/run_eval.py` (config vs execução) e `canonical_ifc.py` (`_build_arg_parser`/`main`).

---

## 6. Loader dos mappings

- Usa **apenas `json` e `pathlib`**. Sem loader em `canonical/mappings/`, sem `__init__.py` novo, sem subpacote importável.
- Diretório dos mappings:
  ```python
  MAPPINGS_DIR = Path(__file__).resolve().parents[1] / "canonical" / "mappings"
  ```
  (a partir de `backend/ingestion/index_lifecycle.py`, `parents[1]` == `backend/`.) **Não** importar `canonical.schema` apenas para localizar os ficheiros.
- O **filename vem exclusivamente do registry fixo** (§3): impossível path traversal, impossível fornecer um path arbitrário.
- Após carregar, **validar** `_meta.record_type == <esperado>` (defesa em profundidade); divergência → `MappingLoadError`.
- **Não modificar** o JSON carregado (dados imutáveis).
- Erros de leitura/parse → `MappingLoadError` (mensagem sem paths sensíveis nem bodies).

---

## 7. Settings operacionais mínimos

- Estrutura **tipada e imutável** local ao lifecycle (**frozen dataclass** `IndexSettings`), **sem novas variáveis de ambiente** nesta issue.
- Defaults: `number_of_shards = 1`, `number_of_replicas = 0`, `mapping.total_fields.limit = 1000`.
- Fornecidos **apenas no create-index**, nunca escritos nos JSON:
  ```json
  {
    "settings": { "index": { "number_of_shards": 1, "number_of_replicas": 0, "mapping.total_fields.limit": 1000 } },
    "mappings": { "…mapping HBIM-020 carregado tal-e-qual…" }
  }
  ```
- **Proibido**: `knn`, `analysis`, `normalizer`, `dimension`, qualquer setting vetorial.
- **Os quatro JSON não são alterados** (a HBIM-020 mantém `{_meta, dynamic, _source, properties}` e byte-stability; acrescentar `settings` partiria os testes offline da HBIM-020).
- Overrides de settings são possíveis por flags da CLI (§14), sem env.

---

## 8. Criação física (não destrutiva, idempotente)

`create_physical_index(client, record_type, physical_version, settings, *, dry_run=False)`:

1. **Validar** `record_type` (registry) e `physical_version` (inteiro positivo).
2. **Compor** o nome físico `<alias>_v<N>`.
3. **Verificar colisões** (§8.1).
4. Se **não existir** → criar com `{settings, mappings}` (mapping carregado do registry). Resultado `CREATED`.
5. Se **existir e for compatível** (§9) → **no-op idempotente**. Resultado `ALREADY_EXISTS_COMPATIBLE`.
6. Se **existir mas incompatível** (§9) → `IncompatibleIndexError` (**fail closed**).
7. **Nunca** `indices.delete`.
8. **Nunca** substituir o mapping de um índice existente.
9. **Nunca** promover automaticamente.
10. `dry_run=True` → não chama o servidor para criar; devolve `DRY_RUN` com o plano (nome físico, settings, record_type).

Resultados **tipados** (enum): `CREATED`, `ALREADY_EXISTS_COMPATIBLE`, `DRY_RUN`.

### 8.1 Colisões
- Se existir um **índice concreto** com o nome do **alias** (`hbim_elements` como índice, não alias) → `AliasConflictError` (o OpenSearch nunca permitiria a promoção; detetar cedo).
- O nome físico `<alias>_v<N>` nunca deve colidir com um alias existente (o lifecycle só cria índices concretos com sufixo `_v<N>`).

---

## 9. Compatibilidade de mapping (comparação recursiva)

**Não** usar igualdade byte-a-byte. **Não** comparar apenas nomes top-level. A verificação compara o **contrato semântico** entre o **mapping local** (JSON carregado) e o **mapping efetivo** devolvido por `client.indices.get_mapping(index=<físico>)`.

### 9.1 O que é comparado
Recursivamente, sobre:
- `_meta` (igualdade exata — o OpenSearch preserva `_meta` verbatim; inclui `record_type`, `mapping_version`, `canonical_schema_versions`, `created_by`);
- `dynamic`;
- `_source` (`enabled`);
- `properties` e, para **cada** campo: `type`, `fields` (multifield), `enabled`, `index`, `coerce`, `ignore_above`, `dynamic`, e a subárvore `properties` de objetos/nested.

### 9.2 Normalização (ignorar apenas defaults do servidor)
Só se ignoram representações/omissões que **provadamente não alteram o contrato**:
- `_source` ausente ⟺ `{"enabled": true}`;
- `index` ausente ⟺ `true`;
- `coerce` ausente ⟺ `true` (campos numéricos);
- `enabled` ausente ⟺ `true`;
- `ignore_above` ausente ⟺ sem limite;
- `type` ausente num nó **com `properties`** ⟺ `object`.

### 9.3 Fail-closed
**Qualquer** divergência de **tipo**, **object↔nested**, **strictness** (`dynamic`), **multifield** (`fields`), **`enabled`**, **`index`** ou **`coerce`** → `IncompatibleIndexError`. A comparação é uma função **pura** `is_mapping_compatible(local, effective) -> bool` (ou que levanta), testável offline com pares sintéticos (§13).

---

## 10. Promoção atómica (single-alias)

`promote(client, record_type, physical_version, *, dry_run=False)`:

### 10.1 Pré-condições (todas fail-closed)
- Alias **não** colide com índice concreto (§8.1) → senão `AliasConflictError`.
- Target físico **existe** → senão `MissingIndexError`.
- `_meta.record_type` do target **corresponde** ao record type → senão `RecordTypeMismatchError`.
- Mapping do target **compatível** (§9) → senão `IncompatibleIndexError`.
- Alias **ausente** (0 targets) **ou** com **exatamente 1** target. **> 1 target** → `AliasConflictError` (§12); **não reparar silenciosamente**.
- Target atual **igual** ao pedido → **no-op idempotente** (resultado `ALREADY_CURRENT`).

### 10.2 Operação
Uma **única** chamada `client.indices.update_aliases(body={"actions": [...]})`. **Nunca** `delete_alias` seguido de `put_alias` (janela sem alias proibida).
- Alias ausente → `[{"add": {"index": <novo>, "alias": <alias>, "is_write_index": true}}]`.
- Swap → `[{"remove": {"index": <atual>, "alias": <alias>}}, {"add": {"index": <novo>, "alias": <alias>, "is_write_index": true}}]`.
- A adição usa **sempre** `is_write_index: true`.

### 10.3 Pós-condição
- Validar `acknowledged == true` da resposta; senão `AliasPromotionError`.
- **Verificação pós-operação**: reler o alias e confirmar que aponta **exclusivamente** ao target novo; estado inesperado → `AliasPromotionError` (fail closed).

O **plano de ações** é construído por uma função **pura** `build_promote_actions(alias, current_targets, new_index) -> list[dict]`, testável sem cliente.

---

## 11. Promoção dos quatro aliases (`promote-all`)

1. **Validar antecipadamente** os quatro targets (existência, `_meta.record_type`, compatibilidade, colisões, contagem de targets).
2. Só depois, **construir um único conjunto de ações** (remoções + adições dos quatro aliases).
3. Executar **uma única** chamada `update_aliases` → **ou os quatro mudam, ou nenhum muda** (o `_aliases` é atómico/all-or-nothing).
4. Verificação pós-operação dos quatro aliases.

Se **qualquer** validação falhar, **nenhuma** ação é enviada (sem promoção parcial). **Não** criar release manifest nesta issue; a coerência vem da chamada atómica única com uma `--physical-version` comum (ou, opcionalmente, por record type — mas sempre uma só chamada).

---

## 12. Alias com múltiplos targets (decisão obrigatória)

- **0 targets** → primeira promoção (add-only).
- **1 target** → swap (ou no-op idempotente se já for o pedido).
- **> 1 target** → **`AliasConflictError`** (fail closed). **Não** remover automaticamente múltiplos targets.

Uma futura operação **explícita** de *repair* fica **fora** desta issue.

---

## 13. Rollback

`rollback(client, record_type, physical_version, *, dry_run=False)`:

- **Exige target explícito** (`--physical-version <N>`). **Não** inferir "a versão anterior" (exigiria estado/histórico persistido — scope creep).
- Validar que o target **existe**, `_meta.record_type` **corresponde**, mapping **compatível** (§9) — mesmas pré-condições da promoção.
- Usa a **mesma primitive atómica** (§10.2): remove o target atual, adiciona o alvo de rollback, numa só chamada.
- **Nunca** apagar o índice que deixa de estar ativo.
- **Manter todas** as versões físicas (v1 e v2 coexistem).

`rollback-all` pode existir: exige `--physical-version` **explícita**, valida os quatro antecipadamente e faz **uma única** operação multi-alias (idêntico a `promote-all`, §11).

Rollback e promote **partilham a primitive**; a diferença é apenas operacional (intenção do operador) — ambos exigem que o target já exista.

---

## 14. Status

`status(client, record_type=None) -> StatusReport` — **determinístico, ordenado, sem timestamps**. Por record type:

| Campo | Fonte |
|---|---|
| `record_type` | registry |
| `alias` | registry |
| `current_target` | `get_alias` (ou `null` se ausente) |
| `is_write_index` | metadados do alias |
| `mapping_version` | `_meta.mapping_version` do target |
| `canonical_schema_versions` | `_meta.canonical_schema_versions` do target |
| `physical_indices` | lista ordenada de `<alias>_v*` existentes |
| `alias_missing` | `true` se o alias não existir |
| `conflicts` | lista (§14.1) |

### 14.1 Conflitos detetados
- `alias_concrete_index_collision` (existe índice concreto com o nome do alias);
- `multiple_targets` (> 1 target no alias);
- `record_type_mismatch` (target com `_meta.record_type` errado);
- `incompatible_mapping` (target incompatível, §9);
- `alias_missing`.

### 14.2 Segredos
**Nunca** mostrar `host`, `port`, `username`, `password`, credenciais nem configurações operacionais sensíveis. Suporta **output JSON estável** (`--json`) e um resumo humano determinístico.

---

## 15. CLI (`python -m ingestion.migrate`)

```
python -m ingestion.migrate create       --record-type element --physical-version 1 [--shards N] [--replicas N] [--total-fields-limit N] [--dry-run]
python -m ingestion.migrate create-all    --physical-version 1 [settings…] [--dry-run]
python -m ingestion.migrate promote        --record-type element --physical-version 1 --yes [--dry-run]
python -m ingestion.migrate promote-all    --physical-version 1 --yes [--dry-run]
python -m ingestion.migrate rollback       --record-type element --physical-version 1 --yes [--dry-run]
python -m ingestion.migrate rollback-all   --physical-version 1 --yes [--dry-run]
python -m ingestion.migrate status         [--record-type element] [--json]
```

- Subparsers com `dest="command", required=True`; `--record-type` restrito aos quatro; `--physical-version` inteiro positivo.
- **Todos os comandos mutantes** suportam `--dry-run` (imprime o plano determinístico, **não** chama o servidor para mutar).
- `create` / `create-all` **não** exigem confirmação (não apagam nem substituem).
- `promote` / `rollback` (e `-all`) **exigem `--yes`** em execução não interativa, ou confirmação TTY.
- **Exit codes:** `0` sucesso; `1` `IndexLifecycleError` (qualquer subclasse); `2` erro de argumentos/configuração (argparse/uso).
- `main(argv: list[str] | None = None) -> int`; `raise SystemExit(main())`. Testável via `main([...])`.

---

## 16. Exceções e exit codes

Hierarquia (base + subclasses; padrão de `CanonicalExtractionError`/`PropertyFactError`):

- `IndexLifecycleError(Exception)` — base;
- `UnknownRecordTypeError`;
- `InvalidPhysicalVersionError`;
- `MappingLoadError`;
- `IncompatibleIndexError`;
- `MissingIndexError`;
- `RecordTypeMismatchError`;
- `AliasConflictError`;
- `AliasPromotionError`.

As mensagens **não** incluem credenciais, hosts, nem bodies completos de respostas OpenSearch. Mapeamento CLI: qualquer `IndexLifecycleError` → exit `1`; erro de argparse/uso → exit `2`; sucesso → `0`.

---

## 17. Legacy `create_index` (neutralização mínima)

**Correção à auditoria:** `index_data()` **é** um caller de produção de `create_index()` — quando o índice legacy está **ausente** (`index_to_opensearch.py:270–271`, `if not client.indices.exists(...): create_index(client)`). **Não** existem outros callers conhecidos (`run_eval` apenas faz snapshot/restore do módulo na sua janela de isolamento; não chama `create_index`).

### 17.1 Alteração mínima (Opção A — create-if-absent idempotente)
No topo de `create_index`, **antes** de `_validate_embedding_dim()`:
- se o índice legacy **já existir** → **retorna imediatamente** (sem `indices.delete`, sem `indices.create`, sem validar dimensão);
- se **não existir** → mantém a criação legacy atual (validação de dimensão + `indices.create`), **sem** o `indices.delete` destrutivo.

Remover o bloco `if exists: delete` (`index_to_opensearch.py:171–173`). **Não** alterar embeddings, mapping legacy, `_id` ou bulk indexing; **não** mover o legacy para os novos aliases. **API/retrieval continuam a usar `bim_elements`**.

### 17.2 Teste de regressão (offline, cliente fake — nunca carrega modelo)
- índice existente → **nenhum** `indices.delete`;
- índice existente → **nenhum** `indices.create`;
- índice existente → **nenhuma** validação de dimensão necessária (short-circuit antes de `_validate_embedding_dim`);
- índice ausente → a criação **continua a funcionar** (chama `indices.create`);
- **nunca** carrega o `SentenceTransformer` nos testes de lifecycle.

---

## 18. Import-safety

- Importar `ingestion.index_lifecycle` e `ingestion.migrate` **não** abre socket, **não** cria cliente, **não** instancia `OpenSearchSettings`, **não** lê `.env` (para além do `load_dotenv()` já existente em `shared.config`, que é neutralizado nos testes).
- O loader ancora via `__file__` (§6) — **não** importa `canonical.schema`.
- As funções de lifecycle recebem o cliente **injetado**; `migrate.py` constrói o cliente **em runtime**, dentro da execução do comando.
- Verificado por subprocess/asserções de guarda de rede, no padrão da suite existente.

---

## 19. Testes offline (sem OpenSearch) — obrigatórios

`backend/tests/test_index_lifecycle.py`:

1. **Registry exato** (quatro record types; sem chunks).
2. **Aliases exatos** (`hbim_elements`/`hbim_property_facts`/`hbim_classification_facts`/`hbim_documents`).
3. **Quatro mappings** referenciados existem e correspondem ao registry.
4. **Ausência de chunks** (nenhum `chunks` no registry).
5. **Physical naming** (`<alias>_v<N>` correto por record type).
6. **Versões inválidas** (`0`, negativa, não-inteiro, excesso) → `InvalidPhysicalVersionError`.
7. **Loader determinístico** (mesmo input → mesmo dict; JSON não mutado).
8. **Traversal impossível** (record_type fora do registry → `UnknownRecordTypeError`; nenhum path externo aceite).
9. **`_meta.record_type`** validado pelo loader (mismatch sintético → `MappingLoadError`).
10. **Settings sem vetores** (`IndexSettings` não contém `knn`/`analysis`/`normalizer`/`dimension`; defaults 1/0/1000).
11. **Comparação recursiva** de mappings: idênticos (módulo defaults do servidor) → compatível.
12. **Mudança de `type`** → `IncompatibleIndexError`.
13. **Mudança nested↔object** → `IncompatibleIndexError`.
14. **Mudança de `dynamic` strict** → `IncompatibleIndexError`.
15. **Mudança de `coerce`** → `IncompatibleIndexError`.
16. **Mudança de multifield / `enabled` / `index`** → `IncompatibleIndexError`.
17. **Create idempotente** (com cliente fake: existente+compatível → `ALREADY_EXISTS_COMPATIBLE`, sem create).
18. **Create incompatível** (fake: existente+incompatível → `IncompatibleIndexError`, sem delete).
19. **Planos promote/rollback** (funções puras: primeira promoção = add-only; swap = remove+add com `is_write_index:true`).
20. **Múltiplos targets** → `AliasConflictError`.
21. **`promote-all` constrói uma única operação** (uma só lista de ações para os quatro aliases).
22. **Status determinístico** (ordenado, sem timestamps; segredos ausentes).
23. **Import-safety** (import não cria cliente/rede; sem `OpenSearchSettings`).
24. **Legacy sem delete destrutivo** (§17.2).

Testes com cliente **fake/injetado** (gravador de chamadas) e funções **puras**; **sem** OpenSearch, **sem** modelo ML.

---

## 20. Testes de integração (Testcontainers OpenSearch 2.19.1) — obrigatórios

`backend/tests/integration/test_index_lifecycle_apply.py`, marcado `integration`, usando **exclusivamente** o fixture local (`opensearch_client`): host/porta do container, `use_ssl=False`, sem credenciais, loopback-only. **Nunca** `OpenSearchSettings`, `.env`, `OPENSEARCH_HOST` operacional, `patrimonio360.webredirect.org`, nem cluster remoto.

1. Criar quatro `_v1`.
2. Validar mappings, **settings** e `_meta` dos quatro.
3. Create repetido **idempotente** (sem delete/recreate).
4. **Primeira promoção** dos quatro aliases.
5. Documentos **sintéticos** via aliases (só para o teste).
6. Criar quatro `_v2` (mesmos mappings).
7. Documentos sintéticos **distintos** em `_v2`.
8. **`promote-all` numa única operação** `update_aliases`.
9. Cada alias aponta **exclusivamente** para `_v2`.
10. **`is_write_index`** correto no target ativo.
11. Escrita/leitura **via alias**.
12. **`rollback-all`** para `_v1`.
13. `_v1` **e** `_v2` continuam a existir.
14. **Nenhum** índice físico é apagado pelo lifecycle.
15. Promoção para target **inexistente** → falha (`MissingIndexError`).
16. Promoção de **record type incorreto** → falha (`RecordTypeMismatchError`).
17. **Mapping incompatível** → falha (`IncompatibleIndexError`).
18. Alias com **múltiplos targets** → falha (`AliasConflictError`).
19. **Colisão alias/índice concreto** → falha (`AliasConflictError`).
20. **Legacy `bim_elements`** não é apagado nem alterado.
21. **Cleanup** apenas dos índices sintéticos do teste.

> **Nota sobre delete:** a eliminação no **teardown** dos testes é permitida e **obrigatória** para cleanup. A proibição de `delete` aplica-se ao **código de produção** (lifecycle/legacy), **não** ao cleanup Testcontainers.

Sem modelos ML, sem IFC, sem cluster externo.

---

## 21. Baseline e regressão

Exigir, sem exceção:
- **HBIM-005 baseline byte-idêntica** (`backend/eval/baselines/current_system.json`);
- **Mappings HBIM-020 byte-idênticos** (os quatro JSON);
- **`backend/canonical/{schema,ids,serialization}.py` byte-idênticos**;
- **API/retrieval inalterados**; a baseline continua a usar o **legacy** `bim_elements`;
- **Nenhum novo alias consumido pela API** nesta issue;
- **Sem** modelos ML, **sem** downloads, **sem** IFC real.

---

## 22. Compatibilidade / não tocar

**Não alterar:** os quatro mappings JSON; `backend/canonical/**`; `shared/config.py`; `shared/opensearch.py`; `backend/api/**`; `backend/eval/**` e a baseline HBIM-005; `frontend/**`; `.gitignore`; `local_data/**`; qualquer `.env`.

**Alterar (só na implementação):** `backend/ingestion/index_to_opensearch.py` (neutralização §17); `docs/development/LOCAL_SETUP.md`; `docs/implementation/IMPLEMENTATION_STATUS.md`; e o **gate mypy** (§24).

---

## 23. Ficheiros previstos

**Criar:**
- `backend/ingestion/index_lifecycle.py`
- `backend/ingestion/migrate.py`
- `backend/tests/test_index_lifecycle.py` (offline)
- `backend/tests/integration/test_index_lifecycle_apply.py` (efémero, **obrigatório**)
- esta spec.

**Modificar (só na implementação):**
- `backend/ingestion/index_to_opensearch.py` (neutralização mínima §17)
- `docs/development/LOCAL_SETUP.md` (secção operacional do lifecycle)
- `docs/implementation/IMPLEMENTATION_STATUS.md` (**só no fim**, §25)
- **gate mypy** (§24): `pyproject.toml` **e** `.github/workflows/ci.yml`.

**Não tocar:** listados em §22.

---

## 24. Tooling e CI

- **Ruff** limpo sobre `backend`.
- **Mypy bloqueante:** `ingestion.index_lifecycle` e `ingestion.migrate` são **código de produção novo, totalmente tipado**, e entram no gate bloqueante — como `ifc_properties`/`property_facts` na HBIM-012. O gate é enforçado em **dois** sítios: `pyproject.toml` (`[[tool.mypy.overrides]] disallow_untyped_defs`) **e** `.github/workflows/ci.yml` (lista explícita de ficheiros passada ao `mypy`). **Ambos** têm de incluir os dois módulos, senão o CI não os verifica.
  > **Ponto de review (desvio da lista ratificada):** as decisões ratificadas nomearam apenas `pyproject.toml` para o gate mypy. Como o gate só é efetivo se o `ci.yml` também listar os módulos (precedente HBIM-011/012), esta spec exige **também** editar `.github/workflows/ci.yml`. Assinalado explicitamente para decisão de review; nenhuma outra mudança ao CI.
- **Integração** reutiliza o job existente `integration-opensearch` (Testcontainers); **sem job novo**. `evaluation-opensearch` inalterado. Sem serviços externos, sem ML.

---

## 25. Implementation status

- **Durante esta fase de spec:** **não** atualizar `IMPLEMENTATION_STATUS.md`.
- **Na implementação:** atualizar **apenas no fim**, **depois** de todos os gates passarem. Se houver bloqueio, deixá-lo **inalterado**.

---

## 26. Critérios de aceitação

Cada critério mapeia para teste/ficheiro/evidência.

1. **Quatro aliases** exatos (`hbim_elements`/`hbim_property_facts`/`hbim_classification_facts`/`hbim_documents`); registry fixo; **sem chunks**; **`bim_elements` não reutilizado**. (§3; testes 1–4)
2. **Naming físico** `<alias>_v<N>`; CLI só aceita `--physical-version` inteiro positivo; versão física **não** inferida do mapping file. (§4; testes 5–6)
3. **Loader** `json`+`pathlib`, diretório via `parents[1]/canonical/mappings`, filename do registry, `_meta.record_type` validado, JSON não mutado, sem `__init__.py`. (§6; testes 7–9)
4. **Settings** frozen dataclass 1/0/1000, fornecidos só no create, **sem** `knn`/`analysis`/`normalizer`/`dimension`; JSON HBIM-020 inalterados. (§7; teste 10)
5. **Compatibilidade recursiva** (`_meta`, `dynamic`, `_source`, `properties`, `type`, `fields`, `enabled`, `index`, `coerce`, `ignore_above`, nested/object); defaults do servidor ignorados; qualquer divergência de contrato → `IncompatibleIndexError`. (§9; testes 11–16)
6. **Create idempotente e não destrutivo** (created / already_exists_compatible / dry_run); incompatível fail-closed; **nunca** delete/substituir/promover. (§8; testes 17–18)
7. **Zero `delete` em produção** (lifecycle e legacy); só cleanup Testcontainers apaga. (§17, §20; teste 24; integração 14/20)
8. **Promoção single-alias atómica** (uma `update_aliases`; `is_write_index:true`; verificação pós-operação). (§10; teste 19; integração 4/11)
9. **Promoção multi-alias atómica** (`promote-all` valida tudo antes e faz uma só chamada; ou os quatro mudam ou nenhum). (§11; teste 21; integração 8–9)
10. **Rollback explícito** (target obrigatório; mesma primitive; nunca apaga; versões preservadas). (§13; integração 12–13)
11. **Múltiplos targets fail-closed** (`AliasConflictError`; sem reparação automática). (§12; teste 20; integração 18)
12. **Status determinístico** (ordenado, sem timestamps, sem segredos; deteta colisões/múltiplos/mismatch/incompatível/ausentes; JSON estável). (§14; teste 22)
13. **CLI e exit codes** (`create/create-all/promote/promote-all/rollback/rollback-all/status`; `--dry-run`; `--yes`; `0/1/2`; `main(argv)->int`). (§15)
14. **Exceções** definidas (base + 8 subclasses; sem credenciais/hosts/bodies nas mensagens). (§16)
15. **Import-safety** (nenhum socket/cliente/`OpenSearchSettings` no import; loader sem `canonical.schema`). (§18; teste 23)
16. **Integração local obrigatória** (Testcontainers 2.19.1, loopback, os 21 pontos). (§20)
17. **Legacy preservado** (`create_index` create-if-absent; `bim_elements` intacto; API/retrieval a usar o legacy; sem ML nos testes). (§17; integração 20)
18. **Baseline byte-idêntica** (HBIM-005) e HBIM-020/canonical byte-idênticos. (§21)
19. **Ausência de ML/IFC/remoto** (nenhum modelo, download, IFC real ou cluster externo). (§20–21)
20. **Fronteira HBIM-022** (sem JSONL, projection, bulk, indexers, `_id` final, embeddings, vetores, chunks, conversão do legacy, consumo de aliases pela API). (§1–2)

---

## 27. Riscos residuais

- **Mappings normalizados pelo servidor** → mitigado pela comparação semântica §9 (não byte-a-byte) + testes de pares sintéticos.
- **`is_write_index`** necessário para escrever via alias e para futuros cenários multi-target → sempre definido `true` na adição.
- **Índice concreto com nome de alias** (tampering manual) → detetado e fail-closed (§8.1, §12).
- **Legacy `bim_elements`** continua a servir a API até uma issue posterior (preservado deliberadamente).
- **Gate mypy em dois sítios** (`pyproject.toml` + `ci.yml`) → assinalado (§24) para não deixar CI sem cobertura dos módulos novos.

---

## 28. Fora de scope

JSONL canónico; projection de `PropertyFact`; bulk indexing; indexers separados; política final de `_id`; embeddings/`knn`/dimensão; chunks; reindex denso; conversão de `bim_elements`; retrieval/API a consumir os aliases; alteração de `backend/canonical`/schema; baseline HBIM-005. Tudo isto é **HBIM-022** ou fases posteriores.

---

## 29. Questões (decisão de review)

Nenhuma bloqueia a implementação; todas ratificadas: (a) quatro record types, aliases `hbim_*`, sem chunks, `bim_elements` não reutilizado; (b) `--physical-version` inteiro, nome físico composto pelo registry; (c) dois módulos (`index_lifecycle` puro + `migrate` CLI), cliente injetado; (d) loader `json`+`pathlib` via `parents[1]`, sem importar `canonical.schema`; (e) settings frozen dataclass no create, sem env, sem vetores, JSON intactos; (f) compatibilidade recursiva semântica, não byte-a-byte; (g) create não destrutivo idempotente; (h) promoção single/multi-alias numa só `update_aliases`; (i) rollback explícito, sem auto-anterior; (j) múltiplos targets fail-closed, sem repair; (k) status determinístico sem segredos; (l) legacy Opção A (create-if-absent); (m) **único desvio à lista ratificada**: `ci.yml` também é editado para o gate mypy ser efetivo (§24), assinalado para review.
