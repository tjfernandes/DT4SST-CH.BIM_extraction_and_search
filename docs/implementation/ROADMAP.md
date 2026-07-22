# Plano de implementação HBIM RAG — auditoria, conflitos e roadmap executável (v3)

> **Papel deste documento.** Especificação de engenharia derivada de (a) `hbim_rag_decisoes_claude.md` (decisões arquiteturais **assumidas**, tratadas aqui como dadas) e (b) auditoria do código atual do repositório `tjfernandes/DT4SST-CH.BIM_extraction_and_search`. O objetivo é que outra sessão de implementação consiga executar cada milestone/issue **sem voltar a decidir a arquitetura fundamental**.
>
> **Decisões arquiteturais preservadas (não negociáveis nesta revisão).** OpenSearch + Neo4j; router determinístico; índices especializados; Qwen3 embeddings/reranker; EvidencePack; AMALIA apenas *grounded*; pipeline multimodal com verificação. A v3 **não** altera nada disto.
>
> **Correções aplicadas na v3 (só estas).** (1) `OPENSEARCH_VERIFY_CERTS` default seguro `true`; um ambiente específico pode definir `false` explicitamente. (2) SSL inferido **apenas** a partir do `scheme` quando `OPENSEARCH_USE_SSL` não está definido — nunca a partir da existência de credenciais. (3) Configuração OpenSearch validada só pelos consumidores que dela precisam; o extractor IFC funciona sem password e sem cliente OpenSearch. (4) Nenhum cliente de rede é criado durante *imports*. (5) HBIM-002 passa a incluir o bootstrap mínimo de pytest + `test_config`; HBIM-004 fica com CI, ruff, mypy, testcontainers e compose. (6) HBIM-032 (gestor de residência completo) depende de HBIM-051. (7) Novo **HBIM-005** antes de qualquer alteração de retrieval: *evaluation harness*, dataset inicial e **baseline do sistema atual**. (8) HBIM-060 passa a **expandir** o harness e aplicar *regression gates*, em vez de o criar. (9) A dimensão de embedding **deixa de estar fixada em 4096 para todos os índices**: benchmark obrigatório entre 1024/2048/4096 e seleção **por índice** (qualidade × armazenamento × latência), mantendo Qwen3-Embedding-8B. (10) Impacto da autenticação (HBIM-003) no frontend explicitado em **003A/003B**. (11) Endpoint, username e detalhes operacionais reais removidos — só exemplos fictícios. (12) Numeração preservada; dependências e sequência atualizadas.
>
> **Convenção de valores.** Todos os hosts/portas/utilizadores neste documento são **fictícios** (`opensearch.example.internal`, `os_service_user`, etc.). Valores operacionais reais vivem apenas em `.env` local / secret manager, **fora** do controlo de versões.
>
> **Convenção de rótulos.** `[OBS]` = facto observado diretamente no código auditado. `[DOC]` = decisão já assumida no documento de decisões. `[REC]` = recomendação/proposta nova desta análise (novo componente, a assinalar como tal). `[GAP]` = lacuna. `[CONFLITO]` = incompatibilidade concreta entre código e documento, com justificação técnica.

---

## 0. Nota metodológica e âmbito da evidência

**Fontes efetivamente auditadas** (código-fonte real, via base de conhecimento do projeto):
`backend/shared/config.py`, `backend/shared/opensearch.py`, `backend/api/main.py`, `backend/api/search.py`, `backend/api/prompts.py`, `backend/ingestion/extract_bim.py`, `backend/ingestion/index_to_opensearch.py`, `README.md`, e a decisão-mãe `hbim_rag_decisoes_claude.md`.

**Não inspecionado em detalhe** (assinalado para não inventar comportamento): `frontend/src/*` (sabe-se apenas que é React+Vite, do README), `backend/environment.yml`, e eventuais scripts auxiliares. **Não observei diretório nem ficheiros de testes** na estrutura descrita no README — o plano assume que a suite de testes é criada de raiz. Toda afirmação `[OBS]` abaixo está ancorada em código lido; onde a leitura foi parcial, o texto di-lo explicitamente.

---

## 1. Auditoria do estado atual

### 1.1 Fluxo de extração IFC — `ingestion/extract_bim.py`

- `[OBS]` Ponto de entrada `extract_bim_data(ifc_file, project_id=None)`. Abre o IFC com `ifcopenshell.open` e itera **apenas** `ifc.by_type("IfcElement")`.
- `[OBS]` Por elemento produz um dicionário com: `id` (=`element.GlobalId`), `project_id`, `project_name`, `ifc_class` (=`element.is_a()`), `name`, `spatial_hierarchy.{storey_name, storey_id, parent_element_id}` (via `ifcopenshell.util.element.get_container` e `get_aggregate`), `material` (via `get_material_name`), `documents` (via `get_associated_documents`, lê `IfcRelAssociatesDocument`), `classifications` (via `get_classifications`, lê `IfcRelAssociatesClassification`), `properties`=psets, `quantities`=qtos (ambos via `get_psets` + `sanitize_keys`), e `metrics.{area,volume,height,thickness}`.
- `[OBS]` `metrics` derivam de heurística por nomes de chave: constantes `KEYS_AREA/KEYS_VOLUME/KEYS_HEIGHT/KEYS_THICKNESS` + `get_normalized_value` + conversão de unidades (`_length_unit_to_m_factor`).
- `[OBS]` `build_semantic_text(element_data)` concatena `project`, `name`, `ifc_class` (enriquecido com rótulos PT/EN de `IFC_CLASS_SEMANTIC_LABELS`), `materials`, `documents` e `properties` numa **única string** `semantic_text`.
- `[GAP]` **Sem extração geométrica.** Não há cálculo de bounding box, centróide, footprint, orientação nem malha. `metrics` são escalares de pset/qto, não geometria real. Isto bloqueia diretamente as relações espaciais do documento (§4.1 `geometry`, §7.4).
- `[GAP]` `IfcSpace` não é capturado como registo próprio: `by_type("IfcElement")` exclui `IfcSpatialStructureElement`. Só o *storey* de contenção fica registado. O campo `space_id/space_name` decidido em `hbim_elements_v2` não tem fonte no extractor atual.
- `[GAP]` Documentos são extraídos como **metadados** (name/description/location/id), sem download, parsing, OCR nem chunking — coerente com o problema #4 do documento.

### 1.2 Canonical model atual

- `[OBS]` O "modelo canónico" atual é **implícito**: o schema JSON produzido por `extract_bim_data` e depois massajado por `index_to_opensearch.sanitize_element`. Não existe um contrato de dados versionado (dataclass/Pydantic/JSON Schema) partilhado entre extração e indexação.
- `[OBS]` `sanitize_element` faz normalização ad-hoc: força `material` a lista, minúsculas em `id/project_id/storey_id/parent_element_id/classification.source/classification.code`, coage `metrics` a float, e garante `semantic_text`.
- `[GAP]` Não há separação entre "elemento", "facto de propriedade", "documento", "chunk", "media" ou "objeto museológico" — tudo vive num único registo de elemento. É exatamente a monólita que o documento decide desmontar (§4).

### 1.3 Mapping e índices OpenSearch — `ingestion/index_to_opensearch.py::create_index`

- `[OBS]` **Um único índice** `OPENSEARCH_INDEX` (default `bim_elements`).
- `[OBS]` `settings`: `number_of_shards:1`, `number_of_replicas:0`, `mapping.total_fields.limit:10000`, `knn:true`, normalizer `lc` (lowercase).
- `[OBS]` `mappings.dynamic: "strict"` no topo, **mas**:
  - `properties: {type: object, dynamic: True}`
  - `quantities: {type: object, dynamic: True}`
  - `property_units: {type: object, dynamic: True}`
  - `quantity_units: {type: object, dynamic: True}`
- `[OBS]` `classifications`: `nested` (`source` keyword, `code` keyword, `name` **text**).
- `[OBS]` `documents`: `{type: object, enabled: False}` → armazenado mas **não pesquisável/indexado**.
- `[OBS]` `semantic_embedding`: `knn_vector`, `dimension=EMBEDDING_DIM`, método `hnsw`, engine `lucene`, `space_type: cosinesimil`, `ef_construction:128`, `m:24`.
- `[OBS]` `create_index` faz `indices.delete` seguido de `indices.create` — **recriação destrutiva** a cada execução. Sem alias, sem reindex, sem versionamento de índice.
- `[OBS]` `_id` do documento = `f"{project_id}_{id}"` (ambos minúsculas).

### 1.4 Criação de embeddings

- `[OBS]` Indexação: `index_to_opensearch.get_embedding_model()` carrega `SentenceTransformer(EMBEDDING_MODEL_NAME)` **in-process**, com `torch_dtype=bfloat16` se CUDA disponível; `generate_embeddings` usa `encode_document` (fallback `encode`) com `normalize_embeddings=True` e `truncate_dim=EMBEDDING_DIM`.
- `[OBS]` Query time: `search.get_query_embedding()` carrega um **segundo** `SentenceTransformer` in-process (`encode_query` fallback `encode`) — modelo duplicado entre indexer e API.
- `[OBS]` Modelo default `zeroentropy/zembed-1`, `EMBEDDING_DIM` default 640; `_validate_embedding_dim` restringe a `SUPPORTED_EMBEDDING_DIMS={40,80,160,320,640,1280,2560}` (conjunto Matryoshka do zembed).
- `[OBS]` **Um vetor por elemento**, sobre o `semantic_text` blended (problema #2 do documento).

### 1.5 Retrieval estruturado e semântico — `api/search.py::build_opensearch_query`

- `[OBS]` Filtros efetivamente aplicados à query: `ifc_class` (`term`/`terms` via `IFC_CLASS_VARIANTS`), `project_id` (`term`), e condições numéricas (`conditions`) sobre `metrics.<campo>` + `metric_fallbacks` (para `quantities.*` e `properties.Pset_WallCommon.*`).
- `[CONFLITO/GAP]` **`material`, `storey` e `name` existem no `SearchPlan` mas não são aplicados na query** no ramo observado de `build_opensearch_query`. Ou seja: "paredes de pedra no piso 1" filtra apenas `IfcWall` e delega "pedra" e "piso 1" ao pós-filtro por LLM (§1.7). A filtragem determinística que o documento exige (§7.2) **não existe sequer para campos já extraídos**.
- `[OBS]` Ramo semântico: se `search_strategy=="semantic"` e há `query_embedding`, monta `{"knn": {"semantic_embedding": {vector, k, filter?}}}` com pré-filtro `bool` (filter+must) opcional. Caso contrário, `bool` com `must` (ou `match_all`) + `filter`.
- `[OBS]` `execute_search` corre contra `OPENSEARCH_INDEX`; `fetch_by_id` faz `get` por `_id` para o caminho *detail*.

### 1.6 Chamadas ao AMALIA (LLM)

- `[OBS]` `search.get_response` → `openai.chat.completions.create` (`temperature=0.1`, system prompt "assistente BIM"), com retry sem `response_format` em `BadRequestError`.
- `[OBS]` Caminho de uma query (`main.chat_endpoint`) usa LLM em, no pior caso (rota *semantic* com histórico): `REWRITE_QUERY` → `CLASSIFY_INTENT` → `EXTRACT_IFC_CLASS` → `EXTRACT_FILTERS` → `EXTRACT_CONDITIONS` → `EXTRACT_EMBEDDING_QUERY` → `FILTER_RESULTS_BATCH` → `FINAL_RESPONSE_FORMAT`. **≈8 chamadas LLM por pergunta.**
  - Rota *aggregation*: `REWRITE` → `CLASSIFY` → `EXTRACT_AGGREGATION` → `EXTRACT_IFC_CLASS` → `EXTRACT_FILTERS` → `AGGREGATION_RESPONSE_FORMAT`.
  - Rota *detail*: `REWRITE` → `CLASSIFY` → `EXTRACT_DETAIL_REF` → `DETAIL_RESPONSE_FORMAT`.
- `[OBS]` O LLM decide: reescrita de follow-up, classificação de intenção, classe IFC, filtros, condições numéricas, query de embedding, índice de detalhe, **relevância final** e texto final. Praticamente todo o *control flow* e a filtragem passam pelo LLM — precisamente o oposto da §5/§6 do documento.

### 1.7 Classificação e routing

- `[OBS]` Routing feito por `CLASSIFY_INTENT` (LLM) → `search_strategy ∈ {chat, structured, semantic, aggregation, detail}`. Não há função de routing determinística; `references_previous_result`, `contains_global_id`, deteção de contagem/relação espacial/multimodal descritas no documento (§6) **não existem** no código.

### 1.8 Filtragem posterior dos resultados

- `[OBS/CONFLITO]` `main.chat_endpoint` chama `FILTER_RESULTS_BATCH` (LLM) sobre os hits formatados por `format_hits_for_prompt` e mantém apenas `relevant_indices`. É filtragem destrutiva por LLM — o documento (§problema #7, §5.2) manda **remover** isto e substituir por reranker + validação determinística.

### 1.9 Agregações — `api/search.py::build_aggregation_query`

- `[OBS]` `terms` aggregation sobre `AGG_FIELD_MAP`: `material→material` (keyword ✔), `storey→spatial_hierarchy.storey_name` (keyword ✔), `ifc_class→ifc_class` (✔), `project→project_id` (✔), `classification→classifications.name`.
- `[CONFLITO/BUG]` `classifications.name` está mapeado como **`text` dentro de `nested`**. Uma `terms` agg plana sobre um campo `text` (sem `fielddata`) e sem `nested` aggregation **falha ou não devolve buckets corretos**. É um bug concreto a corrigir na normalização (facto: usar `classification_codes`/`classification_text` keyword).
- `[OBS]` `agg_field=="count"` devolve apenas `total` (sem bucket) — correto para contagens globais.

### 1.10 Problemas de segurança

- `[OBS/CRÍTICO]` `shared/config.py`: `OPENSEARCH_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "<redigido — ver secret manager>")`. **Password real hardcoded como default** e presente no repositório. Deve ser rodada imediatamente (é uma credencial comprometida) e o default removido. Corresponde ao problema #8 do documento.
- `[OBS]` `USE_SSL`, `VERIFY_CERTS`, `SSL_SHOW_WARN` default `False` → cliente OpenSearch sem TLS/verificação por omissão.
- `[OBS]` `api/main.py` CORS: `allow_origins=["*"]` com `allow_credentials=True` (combinação inválida/insegura por spec CORS) e **sem autenticação** no endpoint `/chat`.
- `[OBS]` `opensearch.py` cria cliente único global sem pooling explícito, retries ou timeouts configurados.

### 1.11 Problemas de mapping explosion

- `[OBS]` `properties/quantities/property_units/quantity_units` com `dynamic:True` → cada pset/qto novo cria campos de mapping. `mapping.total_fields.limit:10000` é o *band-aid* que o documento (§problema #3) rejeita. IFC do mundo real (Revit/ArchiCAD, psets custom, HBIM patrimonial) gera facilmente centenas de nomes de propriedade distintos → risco de esgotar o limite e degradar o cluster.

### 1.12 Acoplamentos e limitações de escalabilidade

- `[OBS]` Modelo de embeddings carregado **duas vezes** in-process (indexer + API) — impede escalar API e ingestão independentemente e prende a GPU ao processo web.
- `[OBS]` `search.py` importa cliente OpenSearch e cliente OpenAI a nível de módulo (efeitos colaterais no import).
- `[OBS]` `create_index` destrutivo impede reindexação sem downtime.
- `[OBS]` `number_of_replicas:0` → sem redundância.
- `[OBS]` Toda a lógica de negócio vive em `main.chat_endpoint` (função monolítica com múltiplos ramos) — difícil de testar unitariamente; não há camada `retrieval/` separável como o documento propõe (§10).
- `[OBS]` `pagination` reexecuta o plano guardado; `result_ids` são passados pelo cliente (estado no frontend) — acoplamento de estado ao cliente.

---

## 2. Estado atual vs documento de decisões — conflitos e lacunas

A tabela cruza cada decisão `[DOC]` com o que existe hoje `[OBS]`. "Δ" indica trabalho necessário.

| # | Decisão do documento `[DOC]` | Estado atual `[OBS]` | Tipo | Δ |
|---|---|---|---|---|
| 1 | Vários índices (`elements_v2`, `property_facts`, `chunks`, `media`, `documents`) | Índice único `bim_elements` | GAP estrutural | Criar 5 índices + indexers |
| 2 | Sem propriedades dinâmicas no elemento; fact table `property_facts` | `properties/quantities` `dynamic:True` no elemento | CONFLITO (mapping explosion) | Normalizar para factos atómicos |
| 3 | Embedding `Qwen3-Embedding-8B` (dimensão **por índice**: benchmark 1024/2048/4096) | `zembed-1` @ 640; validador só aceita `{40..2560}` | CONFLITO de código | Novo serviço + validador por-modelo + benchmark de dim + reindex |
| 4 | `Qwen3-Reranker-8B`; **remover** filtro por LLM | `FILTER_RESULTS_BATCH` (LLM) ativo | CONFLITO | Reranker + remover pós-filtro LLM |
| 5 | Router **determinístico** antes de qualquer LLM | `CLASSIFY_INTENT` (LLM) | CONFLITO | `retrieval/router.py` determinístico |
| 6 | Documentos como evidência própria (parse+OCR+chunks+vetor) | `documents` `enabled:False`, metadados | GAP | Pipeline Docling/PaddleOCR + `chunks_v1` |
| 7 | Geometria e relações espaciais *first-class* | Sem geometria; só `metrics` de pset | GAP | `geometry_extractor` + edges/Neo4j |
| 8 | Neo4j como fonte de verdade de relações | Inexistente | GAP | `kg_builder` + `graph.py` |
| 9 | Multimodal (`media_v1`, `jina-clip-v2`, VLM verifier, `VISUALLY_MATCHES`) | Inexistente | GAP | Milestone multimodal |
| 10 | `EvidencePack` estruturado; AMALIA só *grounded* | AMALIA decide factos e filtra | CONFLITO | `retrieval/evidence.py` + prompt novo |
| 11 | Filtros determinísticos p/ material/piso/métricas | material/storey/name **não aplicados** na query | CONFLITO/BUG | Implementar filtros lexicais reais |
| 12 | Agregações determinísticas fiáveis | agg de `classifications.name` (text/nested) partida | BUG | Facto keyword + nested agg correta |
| 13 | Segurança: sem segredos hardcoded, arranque falha sem `.env` | Password default hardcoded; CORS `*`+credentials; sem auth | CRÍTICO | Milestone 0 |
| 14 | Avaliação offline com gold set (structured/semantic/historical/spatial/document/visual) | Inexistente | GAP | Harness de avaliação |
| 15 | `kNN` só p/ semântico/visual/documental; resto determinístico | `semantic` usa kNN; estruturado não usa material/storey | Parcial | Router + parser resolvem |

### 2.1 Incompatibilidades concretas de código (com justificação técnica)

Estas são as únicas situações em que o plano **toca** decisões, e apenas por incompatibilidade concreta com o código — nunca revertendo a arquitetura:

1. **Dimensão de embedding vs `_validate_embedding_dim`.** `[CONFLITO]` `SUPPORTED_EMBEDDING_DIMS` é específico do `zembed-1` e não suporta as dimensões-alvo. *Justificação:* migrar para `Qwen3-Embedding-8B` implica **remover/substituir** esse validador pela validação por-modelo, aceitando o conjunto benchmarkado {1024, 2048, 4096} (truncagem Matryoshka nativa do Qwen3), servido (não `SentenceTransformer.truncate_dim` in-process). A dimensão efetiva é escolhida por índice (correção 9, HBIM-031).

2. **Engine kNN e dimensão do vetor.** `[REC]` A decisão de modelo (`Qwen3-Embedding-8B`) é mantida; a **dimensão deixa de estar fixada em 4096 para todos os índices** (correção 9). Justificação: 4096×`float32` = 16 KB/vetor só no denso, mais o grafo HNSW — em `hbim_chunks_v1` (muitos vetores) isto pesa em armazenamento e latência sem ganho garantido de qualidade. Ação: **benchmark obrigatório** entre 1024/2048/4096 (via truncagem Matryoshka do Qwen3-Embedding-8B) por tipo de índice, e **selecionar a dimensão por índice** com base em qualidade (nDCG/Recall no gold) × armazenamento × latência (HBIM-031). Parametrizar também o engine (`lucene`→`faiss`) por métrica, não por antecipação. A dimensão escolhida fica registada no mapping de cada índice.

3. **Recriação destrutiva do índice vs migração exigida.** `[CONFLITO]` `create_index` faz `delete`+`create`. *Justificação:* a estratégia de reindexação/migração pedida (aliases, zero-downtime) é incompatível com apagar o índice a cada corrida. Ação: indexação por alias versionado (`*_vN`) + `reindex`/swap de alias.

4. **Agregação sobre `classifications.name` (text/nested).** `[BUG]` Ação: materializar `classification_codes: keyword[]` e `classification_text: text` no elemento (como o documento já prevê em `hbim_elements_v2`) e agregar sobre o keyword.

5. **`material/storey/name` extraídos mas não filtrados.** `[BUG]` Ação: o novo `query_parser`/`lexical.py` passa a aplicar `terms`/`match` determinísticos sobre `materials`/`storey_name`/`name.keyword`.

Nada acima altera as decisões-mãe (OpenSearch+Neo4j, Qwen3, reranker, router determinístico, índices separados, EvidencePack). São correções de implementação para as tornar realizáveis.

---

## 3. Plano incremental por milestones

Cada milestone é independentemente entregável e deixa o sistema funcional. A ordenação respeita as Fases 0–5 do documento (§10) e refina-as. Complexidade: **S** (≤2 dias), **M** (≤1 semana), **L** (1–2 semanas), **XL** (>2 semanas).

Diretório-alvo (do documento §10), a criar incrementalmente:
```
backend/ingestion/{ifc_extractor,geometry_extractor,document_ingestor,image_ingestor,museum_ingestor,kg_builder}.py
backend/ingestion/indexers/{elements,property_facts,chunks,media,documents}_indexer.py
backend/retrieval/{router,query_parser,lexical,dense,hybrid,graph,multimodal,rerank,evidence}.py
backend/models/{embeddings_qwen3,reranker_qwen3,ocr_paddle,vlm_verifier}.py
backend/api/{main,schemas,responses}.py
backend/shared/{config,opensearch,neo4j,logging,security}.py
backend/canonical/{schema.py, mappings/}
backend/eval/{dataset/, run_eval.py, metrics.py}
backend/tests/...
```

---

### M0 — Segurança, configuração tipada, harness de testes/observabilidade
*(Fase 0 do documento)* — **Complexidade: M**

**Objetivo.** Eliminar o risco de segurança imediato, tornar a configuração determinística e falível, e instalar a base de testes/observabilidade sem a qual nenhum milestone seguinte é verificável.

**Alterações arquiteturais.** Introduzir `pydantic-settings` como fonte de config, **segmentada por consumidor** (correção 3): cada grupo de definições é validado só por quem o usa. Arranque de um consumidor falha se lhe faltar um segredo que ele precise — mas o extractor IFC **não** depende de OpenSearch nem de password. Logging estruturado JSON; `request_id` por pedido; métricas Prometheus. **Nenhum cliente de rede é criado durante *imports*** (correção 4): clientes OpenSearch/OpenAI passam a *lazy singletons* / dependências FastAPI, nunca instanciados a nível de módulo (corrige §1.12).

**Ficheiros a modificar.** `shared/config.py` (remover default de password; settings segmentadas; validação estrita), `shared/opensearch.py` (factory *lazy*; timeouts, retries; **sem** instância global no import), `api/search.py` (remover `opensearch_client = get_opensearch_client()` e cliente OpenAI a nível de módulo), `api/main.py` (CORS restrito por env; `/healthz`,`/readyz`; middleware de `request_id`).

**Novos ficheiros.** `shared/logging.py`, `shared/security.py` (auth por API key/JWT no `/chat`; redação de segredos), `.env.example` (sem segredos), `backend/tests/test_config.py`, `backend/tests/conftest.py`, `backend/pytest.ini` (bootstrap mínimo — correção 5), `observability/` (dashboards). *(compose/CI ficam em HBIM-004.)*

**Interfaces/schemas (settings segmentadas por consumidor — correções 1–4).**
```python
class OpenSearchSettings(BaseSettings):
    # instanciada SÓ por consumidores de OpenSearch (indexers, API de retrieval).
    # O extractor IFC NUNCA a instancia → funciona sem password/cliente (correção 3).
    host: str                                  # ex.: opensearch.example.internal (host puro OU com esquema)
    port: int = 9200
    scheme: Literal["http", "https"] = "https"
    username: str = "os_service_user"          # alias: OPENSEARCH_USER / OPENSEARCH_USERNAME
    password: SecretStr                         # obrigatória p/ ESटE consumidor; nunca no repo
    use_ssl: bool | None = None                # None ⇒ inferir do scheme, e SÓ do scheme (correção 2)
    verify_certs: bool = True                   # default SEGURO (correção 1); ambiente pode pôr false explícito
    ssl_show_warn: bool = False
    model_config = SettingsConfigDict(env_prefix="OPENSEARCH_", env_file=".env",
                                      extra="ignore", populate_by_name=True)

    @property
    def effective_use_ssl(self) -> bool:
        # correção 2: nunca inferir SSL pela existência de credenciais
        return self.use_ssl if self.use_ssl is not None else (self.scheme == "https")

class ApiSettings(BaseSettings):      # auth/CORS/logging — usada pela API
    auth_enabled: bool = True; api_keys: list[SecretStr] = []
    cors_allow_origins: list[str] = []
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

class LlmSettings(BaseSettings):      # usada só por quem chama AMALIA
    model: str; api_key: SecretStr | None = None; base_url: AnyHttpUrl | None = None
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")
```
O extractor importa apenas o que precisa (schema canónico, ifcopenshell); **não** importa `OpenSearchSettings`. Assim, `python -m ingestion.ifc_extractor …` corre sem qualquer variável OpenSearch definida.

**Configuração de ligação OpenSearch (exemplos FICTÍCIOS — correção 11).** Valores reais só em `.env` local/secret manager, fora do versionamento:
```env
OPENSEARCH_HOST=opensearch.example.internal    # host puro; se vier com esquema, é normalizado
OPENSEARCH_PORT=9200
OPENSEARCH_SCHEME=https
OPENSEARCH_USERNAME=os_service_user
OPENSEARCH_PASSWORD=                # ← definir localmente / secret manager; NÃO commitar
# OPENSEARCH_USE_SSL=              # vazio ⇒ inferido do scheme (https ⇒ true); nunca das credenciais
OPENSEARCH_VERIFY_CERTS=true        # default seguro; um ambiente específico pode pôr false explícito
OPENSEARCH_SSL_SHOW_WARN=false
```
Três tarefas concretas de reconciliação com o código atual `[OBS→AÇÃO]`:
1. **Nomes de variáveis divergentes.** O código lê `OPENSEARCH_USER`, `USE_SSL`, `VERIFY_CERTS`, `SSL_SHOW_WARN`; deployments podem usar `OPENSEARCH_USERNAME`, `OPENSEARCH_USE_SSL`, `OPENSEARCH_VERIFY_CERTS`, `OPENSEARCH_SSL_SHOW_WARN`. `OpenSearchSettings` aceita ambos (aliases/`env_prefix`) para não partir nada.
2. **Host com esquema.** `shared/opensearch.py` usa `hosts=[{"host": ..., "port": ...}]`; passar `https://…` nesse formato de dicionário está **errado** (`host` deve ser hostname puro). Ação: normalizar — separar esquema/host, ou usar a forma de URL `hosts=["https://opensearch.example.internal:9200"]`.
3. **Inferência de SSL (correção 2).** Se `OPENSEARCH_USE_SSL` não estiver definido, `effective_use_ssl = (scheme == "https")`. **Nunca** inferir a partir da presença de username/password.

**Índices/mappings.** Nenhum.

**Migrations/reindex.** Nenhuma de dados. **Rotação obrigatória** da password que foi o default hardcoded no repositório (§1.10): tratar como credencial comprometida — rodar no cluster e servir só via `.env`/secret manager. O campo `OPENSEARCH_PASSWORD` fica **vazio** e todos os valores acima são fictícios (correção 11).

**Modelos ML/deployment.** Nenhum.

**Testes unitários (correção 5 — bootstrap mínimo aqui).** `test_config`: (a) consumidor OpenSearch falha sem `password`; (b) **`verify_certs` default `True`**; (c) `use_ssl` não definido ⇒ inferido só do scheme; (d) credenciais presentes com `scheme=http` ⇒ `effective_use_ssl == False` (não infere de credenciais); (e) `extra` não parte com chaves de outros grupos; (f) **o extractor IFC importa e corre sem qualquer env OpenSearch**.

**Testes de integração.** `test_health`: `/healthz` 200; `/chat` sem API key → 401 quando auth ativa. *(testcontainers/compose ficam em HBIM-004.)*

**Avaliação de retrieval.** N/A. *(harness + baseline ficam em HBIM-005.)*

**Observabilidade.** Logs JSON com `request_id`, latência por etapa, contadores de chamadas LLM; `/metrics` Prometheus.

**Critérios de aceitação.**
- Nenhum segredo no repositório (`git grep` limpo; validado por hook/CI).
- Consumidor OpenSearch recusa arrancar sem `OPENSEARCH_PASSWORD`; API recusa sem chave.
- **O extractor IFC corre sem qualquer variável OpenSearch e sem password** (correção 3).
- **Nenhum cliente de rede instanciado no import** (verificável: importar `api.search` não abre ligações) (correção 4).
- `verify_certs` default `True`; SSL inferido só do scheme (correção 1–2).
- `/chat` protegido; CORS não usa `*`+credentials.
- `pytest` + `test_config` correm localmente (bootstrap mínimo). *(CI/lint/type-check em HBIM-004.)*

**Riscos.** Password já comprometida (mitigar: rotação imediata + revisão de acessos). Auth quebra frontend (mitigar: entregar em conjunto com HBIM-003B; chave de dev documentada).

**Dependências.** Nenhuma.

---

### M1 — Representação canónica HBIM (IR) + normalização de propriedades (fact table)
*(fundação da Fase 1)* — **Complexidade: L**

**Objetivo.** Definir o contrato de dados versionado que substitui o schema implícito, e transformar `properties/quantities` dinâmicos em **factos atómicos** — resolvendo o mapping explosion na origem, antes de tocar em índices.

**Alterações arquiteturais.** Introduzir `canonical/schema.py` (modelos Pydantic v2) como fronteira entre extração e indexação. Extractor deixa de emitir um blob por elemento e passa a emitir **streams tipados**: `ElementRecord`, `PropertyFact`, `DocumentRef`, `ClassificationFact`. Normalização (Unicode/acentos/singular-plural, dicionários IFC/materiais) num módulo determinístico.

**Ficheiros a modificar.** `ingestion/extract_bim.py` → refatorar para `ifc_extractor.py` emitindo records canónicos (manter funções `get_material_name`, `get_associated_documents`, `get_classifications`, `sanitize_keys` reaproveitadas).

**Novos ficheiros.** `canonical/schema.py`, `ingestion/ifc_extractor.py`, `ingestion/normalize.py` (dicionários PT/EN de `ifc_class`→`semantic_label`, gazetteer de materiais, thesaurus arquitetónico; fuzzy determinístico), `tests/test_canonical_schema.py`, `tests/test_normalize.py`, `tests/test_property_facts.py`.

**Interfaces/schemas.**
```python
class PropertyFact(BaseModel):
    fact_id: str            # sha1(element_id|pset|prop|value)
    project_id: str; element_id: str; ifc_class: str
    pset: str; property_name: str; property_name_norm: str
    value_raw: str; value_text: str | None; value_number: float | None
    unit: str | None; value_type: Literal["string","number","bool","date","enum"]
    source: Literal["pset","qto","classification","museum","inferred"]
    confidence: float = 1.0

class ElementRecord(BaseModel):
    id: str; project_id: str; project_name: str
    ifc_class: str; name: str; name_normalized: str
    semantic_label: str; materials: list[str]
    storey_id: str | None; storey_name: str | None
    space_id: str | None; space_name: str | None
    parent_element_id: str | None
    classification_codes: list[str]; classification_text: str
    metrics: Metrics
    element_text: str       # curto, controlado (NÃO despeja properties)
```

**Índices/mappings.** Nenhum ainda (M2 consome estes contratos).

**Migrations/reindex.** Script `ifc_to_canonical.py`: IFC → JSONL de records canónicos (`elements.jsonl`, `property_facts.jsonl`, `documents.jsonl`). Idempotente por `fact_id`/`id`.

**Modelos ML/deployment.** Nenhum (regras + dicionários, conforme §5.6 "primeiro regras, depois LLM").

**Testes unitários.** Extração de facto por pset conhecido; dedup por `fact_id`; normalização de material ("Calcário"→"calcario"); `value_type` inferido corretamente; `element_text` não contém dump de properties.

**Testes de integração.** IFC de amostra → contagem estável de elements/facts; golden JSONL comparado byte-a-byte (com tolerância a ordenação).

**Avaliação.** Cobertura de normalização: % de `ifc_class`/materiais mapeados no dicionário vs total observado no IFC de amostra (meta ≥95% classes, ≥85% materiais).

**Observabilidade.** Contadores de records emitidos por tipo; lista de `property_name` não normalizados (para curar dicionário).

**Critérios de aceitação.**
- Zero propriedades dinâmicas em `ElementRecord` (todas as arbitrárias vão para `PropertyFact`).
- Contrato Pydantic valida 100% dos records do IFC de amostra.
- `space_id/space_name` populados quando o IFC tem `IfcSpace` (extração passa a ler espaços).

**Riscos.** Dicionários incompletos para HBIM patrimonial (mitigar: fallback `semantic_label=ifc_class` + relatório de gaps). **Não usar LLM aqui** (regra do projeto).

**Dependências.** M0.

---

### M2 — Índices OpenSearch separados + indexers + migração por alias
*(Fase 1)* — **Complexidade: L**

**Objetivo.** Materializar `hbim_elements_v2`, `hbim_property_facts_v1`, `hbim_documents_v1`, `hbim_chunks_v1` (chunks vazio de PDFs por agora; recebe já o `ifc_semantic`) com mappings sem explosão, e uma estratégia de reindexação segura.

**Alterações arquiteturais.** Um indexer por índice (`indexers/*_indexer.py`). Mappings como ficheiros JSON versionados em `canonical/mappings/`. Alias lógico → índice físico `*_vN`.

**Ficheiros a modificar.** `ingestion/index_to_opensearch.py` → dividir em indexers; **remover** `delete`+`create` destrutivo; `shared/config.py` (nomes de índice/alias por env).

**Novos ficheiros.** `ingestion/indexers/{elements,property_facts,documents,chunks}_indexer.py`, `canonical/mappings/*.json`, `ingestion/migrate.py` (criar `*_v1`, indexar, apontar alias, opcional `reindex`), `tests/test_index_mappings.py`.

**Interfaces/schemas (mapping — decisões §4.1–4.5, com correções).**
- `hbim_elements_v2`: campos de `ElementRecord`; `properties`/`quantities` **removidos**; `classification_codes: keyword[]`, `classification_text: text`; `embedding_qwen3: knn_vector(dim=<selecionada por índice; benchmark 1024/2048/4096 em HBIM-031>)` (adicionado em M3); `evidence_refs.*: keyword[]`; `relations_summary.*: keyword[]` (populado em M7).
- `hbim_property_facts_v1`: mapping estático (todos keyword/text/double), `dynamic: strict`. **Sem** objetos dinâmicos.
- `hbim_documents_v1` e `hbim_chunks_v1`: conforme §4.3/§4.5; `text: text` + `text_exact: keyword`; `embedding_qwen3` adicionado em M3.

**Migrations/reindex.** `migrate.py --create --index elements --version v2`; indexação para `*_v2`; `--promote` faz swap atómico do alias `hbim_elements`→`hbim_elements_v2`. Rollback = repontar alias. **Nunca** apagar índice em uso.

**Modelos ML/deployment.** Nenhum (embeddings entram em M3; até lá indexa-se sem vetor ou com vetor placeholder desativado).

**Testes unitários.** Mapping tem `dynamic:strict` em todos os índices; ausência de `properties` dinâmico; `fact_id`/`chunk_id` são `_id`.

**Testes de integração.** Criar índices em OpenSearch efémero (testcontainers), indexar amostra, validar contagens e round-trip por `_id`; swap de alias sem downtime.

**Avaliação.** N/A (estrutural).

**Observabilidade.** Métrica `mapping.total_fields` por índice (deve manter-se ~constante ao indexar novos IFC — prova de que a explosão foi eliminada).

**Critérios de aceitação.**
- Indexar 3 IFC distintos **não** aumenta `total_fields` de `property_facts` (fact table estável).
- Aliases funcionam; API lê por alias.
- Reindex sem downtime demonstrado em teste.

**Riscos.** Migração de dados existentes (mitigar: `migrate.py` lê índice antigo `bim_elements` e converte via canónico).

**Dependências.** M1.

---

### M3 — Serviço de embeddings Qwen3-Embedding-8B + benchmark de dimensão + reindex denso
*(Fase 1, item 3)* — **Complexidade: L**

**Objetivo.** Substituir `zembed-1@640` por `Qwen3-Embedding-8B` **servido** (não in-process), executar o **benchmark de dimensão** (1024/2048/4096) e reindexar os campos densos de `elements` e `chunks` com a dimensão **selecionada por índice** (correção 9).

**Alterações arquiteturais.** `models/embeddings_qwen3.py` = cliente HTTP para um servidor de inferência dedicado (vLLM/TEI). Indexer e API deixam de carregar `SentenceTransformer`; passam a chamar o serviço. Um único ponto de verdade para dimensão (por índice)/prefixos de instrução.

**Ficheiros a modificar.** `ingestion/index_to_opensearch.py`/indexers (usar serviço; dimensão por índice), `api/search.py::get_query_embedding` → chamar serviço; **remover** `_validate_embedding_dim`/`SUPPORTED_EMBEDDING_DIMS` (substituir por validação por-modelo do conjunto {1024,2048,4096}).

**Novos ficheiros.** `models/embeddings_qwen3.py`, `deploy/embeddings.{Dockerfile,compose}` (vLLM/TEI), `eval/dim_benchmark.py`, `tests/test_embeddings_client.py`.

**Interfaces/schemas.**
```python
class EmbeddingClient(Protocol):
    supported_dims: tuple[int, ...]  # (1024, 2048, 4096) via Matryoshka
    def embed_documents(texts: list[str], dim: int) -> list[list[float]]
    def embed_query(text: str, dim: int, instruction: str | None = None) -> list[float]
```
`knn_vector.dimension` de cada índice é a dimensão **escolhida por benchmark** para esse índice (registada no mapping).

**Migrations/reindex.** Novo `*_v2` denso; reindexação completa (embeddings mudam de espaço → **reindex obrigatório**). Alias swap após validação de qualidade. Reindexar por índice com a dimensão vencedora.

**Modelos ML/deployment.** Ver §5: Qwen3-Embedding-8B, BF16, vLLM. Fallback: TEI/Infinity ou BGE-M3.

**Testes unitários.** Cliente devolve a dimensão pedida (1024/2048/4096); normalização L2; instrução de query aplicada.

**Testes de integração.** Serviço up → embed de lote; latência p50/p95 por dimensão; consistência doc/query (mesma frase ~cosine 1.0).

**Avaliação de retrieval (benchmark de dimensão — correção 9).** Usar o harness/baseline de **HBIM-005**. Para cada índice, medir nDCG@10/Recall@k, tamanho do índice e latência kNN em 1024 vs 2048 vs 4096; **selecionar a dimensão por índice** (ex.: `elements` pode justificar 4096; `chunks` frequentemente 2048/1024). Decisão registada com números.

**Observabilidade.** Latência/throughput do serviço por dimensão; tamanho de cada índice; falhas/timeouts.

**Critérios de aceitação.**
- API e indexer não carregam mais `SentenceTransformer` in-process.
- Benchmark 1024/2048/4096 executado; dimensão **documentada e aplicada por índice**.
- `elements_v2`/`chunks_v1` pesquisáveis por kNN na dimensão escolhida.
- Recall@10 dense ≥ **baseline de qualidade de modelo zembed medida em HBIM-005B** (`eval/baselines/semantic_model_quality.json`). HBIM-005 **não** mediu qualidade de modelo: a sua categoria `semantic_vector` exercita o caminho kNN com vetores sintéticos desenhados à mão (spec HBIM-005 §95/§302, `run_eval.py`: `semantic model quality: not evaluated`), pelo que a sua pontuação **não** é uma baseline de embeddings.

**Riscos.** VRAM/latência do 8B (mitigar: batching, FP8 se necessário — ver §5). Custo de reindex (mitigar: batch offline; o benchmark corre em amostra antes do reindex total).

**Dependências.** M2, HBIM-005 (harness + baseline).

---

### M4 — Router determinístico + query parser + caminho estruturado/agregação/exact sem LLM
*(Fase 1, item 5 + §6)* — **Complexidade: L**

**Objetivo.** Remover o LLM do *control flow*. Uma função `route(query, context) -> Route` testável decide a estratégia; um `query_parser` determinístico extrai `ifc_class`, material, storey, condições numéricas e GlobalId.

**Alterações arquiteturais.** `retrieval/router.py` implementa exatamente as regras §6 (`contains_global_id`, `references_previous_result`, `asks_count_or_distinct`, `has_numeric_condition`, `has_ifc_class`, `has_storey`, `has_material`, `has_spatial_relation_terms`, `mentions_*`). `retrieval/query_parser.py` substitui `EXTRACT_IFC_CLASS/FILTERS/CONDITIONS/AGGREGATION/DETAIL_REF` por regex+dicionários. `lexical.py` aplica **de facto** filtros material/storey/name (corrige §1.5 GAP). `main.chat_endpoint` passa a orquestrar módulos, não prompts.

**Ficheiros a modificar.** `api/main.py` (substituir cascata de prompts por `router`+`query_parser`), `api/search.py::build_opensearch_query` (aplicar material/storey/name), `api/prompts.py` (marcar como deprecados os prompts de parsing/routing/filtragem).

**Novos ficheiros.** `retrieval/router.py`, `retrieval/query_parser.py`, `retrieval/lexical.py`, `tests/test_router.py`, `tests/test_query_parser.py`, `eval/dataset/routing_gold.jsonl`.

**Interfaces/schemas.**
```python
class Route(str, Enum): exact_lookup; aggregation; structured; graph; multimodal; document_hybrid; hybrid_semantic
class ParsedQuery(BaseModel):
    route: Route; global_ids: list[str]; ifc_class: str | None
    materials: list[str]; storey: str | None; conditions: list[Condition]
    agg_field: str | None; refers_previous: bool; raw: str
```

**Índices/mappings.** Aggregação corrigida: `classification` agrega sobre `classification_codes` (keyword); `material` sobre `materials`.

**Migrations/reindex.** Nenhuma.

**Modelos ML/deployment.** **Nenhum LLM no caminho** (AMALIA só para resposta final, ainda via prompt antigo até M5).

**Testes unitários.** Cobertura da tabela de termos §6 (aggregation/structured/spatial/multimodal/historical); GlobalId detetado; "detalha o primeiro" → exact/detail; contagem → aggregation. Property-based: nunca cai em rota inválida.

**Testes de integração.** Query→plano→OpenSearch com filtros material/storey aplicados (regressão do bug §1.5).

**Avaliação de retrieval.** **Routing accuracy** no `routing_gold` (meta ≥95%). Precisão de filtros estruturados (as "paredes de pedra no piso 1" devolvem só pedra+piso1).

**Observabilidade.** Distribuição de rotas por dia; taxa de fallback para `hybrid_semantic`; nº de chamadas LLM por pedido (deve **cair** de ~8 para ≤1–2).

**Critérios de aceitação.**
- 0 chamadas LLM para routing/parsing/filtragem.
- Filtros material/storey/name aplicados na query (teste de regressão verde).
- Routing accuracy ≥95% no gold.

**Riscos.** Regras não cobrem fraseados raros (mitigar: fallback semântico + expansão opcional por AMALIA *depois* do router, §6 "onde AMALIA ainda entra").

**Dependências.** M2 (índices), M3 (dense p/ hybrid_semantic).

---

### M5 — Hybrid retrieval + RRF + Qwen3-Reranker-8B + EvidencePack + AMALIA grounded
*(Fase 2 + §8)* — **Complexidade: XL**

**Objetivo.** Substituir o pós-filtro por LLM por candidate-generation multi-fonte, fusão RRF, reranking cross-encoder e um `EvidencePack` do qual o AMALIA responde de forma *grounded* com citações.

**Alterações arquiteturais.** `retrieval/{dense,lexical,hybrid,rerank,evidence}.py`. `hybrid.py` = BM25 top-200 + dense top-200 → **RRF** → reranker top-N. `rerank.py` = cliente Qwen3-Reranker-8B. `evidence.py` monta `EvidencePack` (dedup + agregação). `api/responses.py` gera resposta grounded. **Remover** `FILTER_RESULTS_BATCH`.

**Ficheiros a modificar.** `api/main.py` (pipeline de retrieval), `api/prompts.py` (novo prompt grounded §9; remover `FILTER_RESULTS_BATCH`, `FINAL_RESPONSE_FORMAT` ajustado a citações), `api/search.py` (BM25 dedicado).

**Novos ficheiros.** `retrieval/dense.py`, `retrieval/hybrid.py`, `retrieval/rrf.py`, `retrieval/rerank.py`, `retrieval/evidence.py`, `models/reranker_qwen3.py`, `api/schemas.py` (`EvidencePack`), `tests/test_rrf.py`, `tests/test_evidence_pack.py`, `tests/test_rerank_thresholds.py`.

**Interfaces/schemas.**
```python
def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]
class EvidenceItem(BaseModel):
    evidence_id: str; source_type: str; source_id: str
    element_id: str | None; document_id: str | None; page: int | None
    text_excerpt: str; score: float; retrieval_method: str; confidence: float
class EvidencePack(BaseModel):
    query: str; route: str; deterministic_filters: dict; result_count: int
    evidence_items: list[EvidenceItem]; graph_paths: list[GraphPath]; caveats: list[str]
```
Pesos iniciais e limiares conforme §8 (por tipo de query).

**Índices/mappings.** Usa `elements`/`chunks` (dense+BM25). Sem novo índice.

**Migrations/reindex.** Nenhuma.

**Modelos ML/deployment.** Qwen3-Reranker-8B (BF16, vLLM/TEI) — ver §5. Fallback: `bge-reranker-v2-m3`.

**Testes unitários.** RRF determinístico (ordenação conhecida); dedup por `evidence_id`; limiar de aceitação (`reranker_score >= threshold` e `source_has_id`); EvidencePack nunca inclui item sem `source_id`.

**Testes de integração.** Query histórica → chunks corretos no top-N pós-rerank; `FILTER_RESULTS_BATCH` ausente do fluxo.

**Avaliação de retrieval.** nDCG@10 e Recall@50 (BM25 vs dense vs RRF vs RRF+rerank) no gold; ganho do reranker mensurável; ausência de "resultados corretos apagados" (comparar com baseline LLM-filter → recall não deve descer).

**Observabilidade.** Scores por método guardados no output (decisão §11.10); latência do reranker; taxa de abstenção.

**Critérios de aceitação.**
- `FILTER_RESULTS_BATCH` removido do código e do fluxo.
- Resposta final contém ids de elemento/documento/página quando existem.
- RRF+rerank ≥ dense-sozinho em nDCG@10 no gold.
- AMALIA responde apenas do EvidencePack (teste: facto ausente do pack → "sem evidência suficiente").

**Riscos.** Latência acumulada (mitigar: reranker só quando >K candidatos, §7.2). Threshold mal calibrado (mitigar: tuning no gold, item HBIM de eval).

**Dependências.** M3, M4.

---

### M6 — Ingestão de documentos (Docling + PaddleOCR-VL) + chunks + entity linking
*(Fase 3)* — **Complexidade: XL**

**Objetivo.** Tornar PDFs/relatórios históricos evidência de primeira classe: parse, OCR, chunking por secção/página com bounding boxes, ligação chunk→elemento, e hybrid retrieval documental.

**Alterações arquiteturais.** `ingestion/document_ingestor.py` (Docling→estrutura/reading-order; PaddleOCR-VL p/ digitalizações), `retrieval/` reutiliza hybrid de M5 sobre `chunks_v1`. Entity linking determinístico (GlobalId/nome/localização) + fuzzy; LLM só para casos não resolvidos (§5.6).

**Ficheiros a modificar.** `ingestion/indexers/chunks_indexer.py`, `ingestion/indexers/documents_indexer.py`, `retrieval/hybrid.py` (source_type=pdf).

**Novos ficheiros.** `ingestion/document_ingestor.py`, `ingestion/chunking.py`, `ingestion/entity_linking.py`, `models/ocr_paddle.py`, `tests/test_chunking.py`, `tests/test_entity_linking.py`, `eval/dataset/document_gold.jsonl`.

**Interfaces/schemas.** `hbim_chunks_v1` (§4.3): `page`, `section_title`, `bbox_on_page`, `element_ids[]`, `source_type=pdf`, `created_by=parser/model/version`. `hbim_documents_v1` (§4.5): `parser`, `ocr_model`, `checksum`, `pages`.

**Migrations/reindex.** Índices `chunks`/`documents` já criados (M2); ingestão incremental idempotente por `checksum`+`chunk_id`.

**Modelos ML/deployment.** PaddleOCR-VL-1.6 (fallback 0.9B), Docling (lib/serviço). Ver §5.

**Testes unitários.** Chunking preserva página/secção; bbox válido; dedup por hash; entity linking liga GlobalId mencionado ao elemento certo.

**Testes de integração.** PDF de amostra → N chunks pesquisáveis; "onde o relatório fala da capela" devolve página/chunk corretos.

**Avaliação de retrieval.** Recall de passagens no `document_gold`; precisão de ligação chunk→elemento.

**Observabilidade.** Páginas/chunks por documento; taxa de OCR vs texto nativo; % chunks ligados a elemento.

**Critérios de aceitação.**
- PDFs pesquisáveis com citação documento+página+chunk.
- `documents` deixa de ser `enabled:False`; evidência documental aparece no EvidencePack.
- ≥ meta de recall no document_gold.

**Riscos.** Layout complexo/plantas (mitigar: ColQwen visual em M8; OCR fallback). Qualidade de digitalizações patrimoniais.

**Dependências.** M2, M3, M5.

---

### M7 — Geometria + relações espaciais + Neo4j KG + graph retrieval
*(Fase 4)* — **Complexidade: XL**

**Objetivo.** Tornar geometria e relações *first-class*: bbox/centróide/orientação por elemento, edges espaciais derivados, KG Neo4j como fonte de verdade de relações, e retrieval por Cypher.

**Alterações arquiteturais.** `ingestion/geometry_extractor.py` (ifcopenshell `geom` + numpy: bbox, centróide, footprint, orientação). `ingestion/kg_builder.py` (nós/relações §4.6). `shared/neo4j.py` (driver). `retrieval/graph.py` (Cypher). `relations_summary` materializado em `elements_v2` para snippets rápidos.

**Ficheiros a modificar.** `ingestion/ifc_extractor.py` (invocar geometria), `indexers/elements_indexer.py` (geometry+relations_summary), `retrieval/router.py` (rota `graph` já existente → liga a `graph.py`), `api/main.py`.

**Novos ficheiros.** `ingestion/geometry_extractor.py`, `ingestion/spatial_relations.py` (containment/adjacency/above-below/intersects via bbox+ifc rel), `ingestion/kg_builder.py`, `shared/neo4j.py`, `retrieval/graph.py`, `tests/test_geometry.py`, `tests/test_spatial_relations.py`, `tests/test_graph_retrieval.py`, `eval/dataset/spatial_gold.jsonl`.

**Interfaces/schemas.** `elements_v2.geometry.{has_geometry,bbox_min[3],bbox_max[3],centroid[3],footprint_area,orientation}`; Neo4j nós/relações §4.6 (`CONTAINS`, `ADJACENT_TO`, `ABOVE/BELOW/INTERSECTS/HOSTED_BY/VOIDS/FILLS`, `MENTIONED_IN`). Edges com `confidence`+`source` (ifc_native vs geom_derived).

**Migrations/reindex.** Reindex de `elements_v2` para acrescentar `geometry`/`relations_summary`. Build inicial do grafo (batch). Idempotente por `global_id`.

**Modelos ML/deployment.** Nenhum ML (geometria e relações são determinísticas). Neo4j em container.

**Testes unitários.** Bbox correto p/ sólido conhecido; adjacência por tolerância; above/below por centróide-z; grafo tem edges IFC nativas (porta-parede).

**Testes de integração.** "o que suporta o telhado?" → caminho de grafo válido; "adjacentes a esta parede" → conjunto correto; edge em falta calculado offline e indexado.

**Avaliação de retrieval.** Precisão/recall de relações no `spatial_gold`; cobertura de geometria (% elementos com bbox).

**Observabilidade.** % elementos com geometria; nº edges por tipo; latência Cypher.

**Critérios de aceitação.**
- Rota `graph` responde com `graph_paths` no EvidencePack.
- Relações IFC nativas + ≥ um tipo de relação derivada de geometria (adjacência) disponíveis.
- Neo4j é fonte de verdade; `relations_summary` em OpenSearch só p/ filtros/snippets (§4.6).

**Riscos.** Custo de tesselação geométrica (mitigar: cache; processar por lote; `iterator` do ifcopenshell). IFC sem geometria (mitigar: `has_geometry:false` + caveat).

**Dependências.** M1, M2.

---

### M8 — Multimodal + VLM verifier + matching visual + museu
*(Fase 5)* — **Complexidade: XL**

**Objetivo.** Ingerir imagens (museu, renders/crops BIM, páginas PDF), embeddings multimodais, **retrieval visual feito por embedders** (jina-clip; ColQwen opcional), e um **VLM apenas como verificador *gated* e leitor de página** — nunca como retriever. Criar `VISUALLY_MATCHES` com score/modelo/evidência, sem assumir match sem limiar (§7.6).

**Separação explícita retrieval ↔ verificação `[REC, do turno anterior]`.**
- **Retrieval/ranking visual = embedders.** Candidatos por `image_embedding_jina_clip_1024` (text→image e image→image); reordenação por late-interaction (ColQwen) quando o layout importa. **Sem VLM aqui.**
- **Gate determinístico antes do VLM.** Filtrar candidatos por metadados (material, período, dimensões, proveniência CIDOC-lite). Só o que passa o gate chega ao VLM. Isto elimina a maioria dos falsos positivos sem custo de GPU.
- **VLM = verificação + leitura.** Decide "corresponde ao mesmo objeto?" antes de escrever a aresta no grafo, e lê a imagem/página para grounding. Corre poucas vezes por sessão.

**Alterações arquiteturais.** `ingestion/{image_ingestor,museum_ingestor}.py`; `models/vlm_verifier.py`; `retrieval/multimodal.py` (embedders + gate); `retrieval/metadata_gate.py` (determinístico). `hbim_media_v1` (§4.4) com `image_embedding_jina_clip_1024` e `caption_embedding_qwen3`. Opcional: ColQwen para páginas PDF visualmente complexas.

**Ficheiros a modificar.** `retrieval/router.py` (rota `multimodal` → `multimodal.py`), `retrieval/evidence.py` (itens visuais + caveat `visual_match_not_confirmed`), `ingestion/kg_builder.py` (`VISUALLY_MATCHES`).

**Novos ficheiros.** `ingestion/image_ingestor.py`, `ingestion/museum_ingestor.py` (normalizador CIDOC-lite), `ingestion/indexers/media_indexer.py`, `models/jina_clip.py`, `models/vlm_verifier.py`, `retrieval/multimodal.py`, `retrieval/metadata_gate.py`, `tests/test_multimodal.py`, `tests/test_metadata_gate.py`, `eval/dataset/visual_gold.jsonl`.

**Interfaces/schemas.** `hbim_media_v1` (§4.4); `museum_object` records; Neo4j `(:MuseumObject)`, `(Element)-[:VISUALLY_MATCHES {score,model,evidence}]->(MuseumObject)`.

**Migrations/reindex.** Novo índice `media`; grafo estende-se com museu.

**Modelos ML/deployment.** jina-clip-v2 (1024) para retrieval; **`Qwen3-VL-8B-Instruct` FP8** como verifier default (co-residente, perfil `P-Online-MM`), **`Qwen3-VL-32B-Instruct` FP8** em escalonamento (perfil exclusivo `P-Verify-Hard`); ColQwen2.5 opcional. Deployment e VRAM geridos pelo gestor de residência (§5.3); o VLM só é carregado quando o gate produz candidatos. Fallbacks e footprints em §5.

**Testes unitários.** Similaridade imagem-imagem; text→image; limiar de aceitação (`image_similarity>=t` **e** `vlm_verifier in {match,probable}` **e** `metadata_conflict==false`).

**Testes de integração.** "elemento parecido com esta peça" → candidatos + verificação; `VISUALLY_MATCHES` só criado acima do limiar com evidência.

**Avaliação de retrieval.** Precisão@k de matching visual no `visual_gold`; taxa de falsos positivos pós-VLM.

**Observabilidade.** Distribuição de scores; % matches confirmados vs rejeitados pelo VLM.

**Critérios de aceitação.**
- Retrieval/ranking visual **não invoca** o VLM (verificável no trace: 0 chamadas VLM antes do gate).
- Gate de metadados descarta candidatos com conflito antes do VLM.
- Nenhum `VISUALLY_MATCHES` sem score/modelo/limiar/evidência.
- Verifier default é `Qwen3-VL-8B` FP8 co-residente; escalonamento para 32B respeita o perfil exclusivo (Emb/Rerank em *sleep*), sem exceder o orçamento de VRAM.
- Retrieval multimodal integrado no EvidencePack com caveats.

**Riscos.** Contenção de GPU na janela de escalonamento (mitigar: gate reduz volume; `P-Verify-Hard` serializado por lock; 8B resolve a maioria). Ruído de matching (mitigar: gate de metadados + VLM verifier + limiar). Modelo VL mais recente à data de implementação (mitigar: `VLM_MODEL` parametrizado; confirmar melhor Qwen-VL disponível no momento).

**Dependências.** M2, M3, M5, M7, e o gestor de residência (HBIM-032).

---

### Milestone transversal — Avaliação e regressão *(criada em HBIM-005, antes de M4; expandida até M8)* — **Complexidade: M (contínuo)**

**Objetivo.** *Evaluation harness* + gold set por categoria (structured, semantic, historical, spatial, document, visual) + **baseline do sistema atual**, criados em **HBIM-005 antes de qualquer alteração de retrieval** (correção 7). A partir daí, **HBIM-060 expande** o harness e aplica *regression gates* em CI (correção 8). O benchmark de dimensão (HBIM-031) consome este harness.

**Novos ficheiros.** `eval/run_eval.py`, `eval/metrics.py` (Recall@k, nDCG@k, MRR, routing accuracy, abstention correctness), `eval/dataset/*_gold.jsonl`, `eval/baselines/current_system.json`, `tests/test_eval_regression.py`.

**Critérios de aceitação.** Baseline do sistema atual medida antes de M4; cada milestone com retrieval só é "done" com o seu slice de gold verde; CI falha em regressão de nDCG/recall/routing > tolerância.

---

## 4. Planos específicos obrigatórios (17)

Cada plano cruza-se com um ou mais milestones. Só se explicita o essencial não-óbvio.

**1. Canonical HBIM intermediate representation.** `[M1]` `canonical/schema.py` (Pydantic v2) como fronteira única extração↔indexação. `ElementRecord`, `PropertyFact`, `DocumentRef`, `ClassificationFact`, `MediaRecord`, `MuseumObject`, `Edge`. Versionado (`schema_version`). Serialização JSONL. Regra: **nada dinâmico** no `ElementRecord`; propriedades arbitrárias só como `PropertyFact`.

**2. Normalização de propriedades IFC sem mapping explosion.** `[M1,M2]` Fact table `hbim_property_facts_v1` com mapping **estático** (`dynamic:strict`). Cada pset/qto → N factos atómicos (`pset`,`property_name`,`value_number|value_text`,`unit`,`value_type`). Elemento guarda só propriedades normalizadas de alto uso. Facetas dinâmicas obtêm-se por agregação sobre `property_name_norm`, não por campos de mapping. Prova de sucesso: `mapping.total_fields` constante ao indexar novos IFC.

**3. Separação de índices.** `[M2,M6,M8]` `hbim_elements_v2`, `hbim_property_facts_v1`, `hbim_chunks_v1`, `hbim_documents_v1`, `hbim_media_v1`; objetos museológicos como records próprios (`museum_object`, em `chunks`/`media` + nó Neo4j); associações em Neo4j (`hbim_kg`). **Sem** parent-child OpenSearch por default (§11.12); ligação por ids + grafo. `nested` só para classificações pequenas (§11.13).

**4. Extração e chunking de PDFs.** `[M6]` Docling para conversão estruturada (reading order, tabelas, secções) → chunking por secção com fallback por página; tamanho-alvo com sobreposição; cada chunk guarda `document_id`,`page`,`section_title`,`bbox_on_page`,`created_by`. Idempotência por `checksum`+`chunk_id`.

**5. OCR e preservação de layout/páginas/bboxes.** `[M6]` PaddleOCR-VL para digitalizações/plantas com texto; guardar `bbox_on_page` por bloco e `page`. Rasterização de páginas → `hbim_media_v1` (`source_type=pdf_page`) para o caminho visual (M8). `ocr_model`/`parser` versionados em `documents`.

**6. Embeddings densos, sparse e multimodais.** `[M3,M6,M8]` Denso: Qwen3-Embedding-8B, **dimensão selecionada por índice** via benchmark 1024/2048/4096 (correção 9; elements/chunks). Sparse: BM25 sempre ativo; neural sparse (rank_features) **só depois da V1** (§5.3) — campo `sparse_features` já reservado no mapping. Multimodal: jina-clip-v2@1024 (`image_embedding`), captions em Qwen3 (`caption_embedding`).

**7. BM25, kNN e hybrid retrieval.** `[M4,M5]` BM25 (lexical.py) e kNN (dense.py) como geradores; hybrid.py combina. `kNN` **só** para rotas semântica/visual/documental (§11.14); estruturado/exact/aggregation usam filtros determinísticos.

**8. Reciprocal Rank Fusion.** `[M5]` `rrf.py` puro e testável (`k=60` default), sobre rankings BM25+dense (+graph/visual quando aplicável). Decisão V1 = RRF (§8); V2 = hybrid pipeline OpenSearch com normalização + pesos por tipo de query (pesos iniciais §8).

**9. Reranking cross-encoder.** `[M5]` Qwen3-Reranker-8B sobre top-100–300 pós-fusão; substitui `FILTER_RESULTS_BATCH`. Limiar afinado no gold (`accept_text_evidence_if reranker_score>=t and source_has_id`). Reranker só quando nº candidatos > K (poupar latência em queries estruturadas).

**10. Retrieval de relações espaciais.** `[M7]` Rota `graph`: resolver entidades → Cypher no Neo4j → se relação inexistente, calcular offline via geometria e indexar edge (§7.4). Resposta com `graph_paths`. Dense só para resolver entidade mencionada vagamente.

**11. Knowledge graph (se adotado — o documento adota).** `[M7,M8]` Neo4j `hbim_kg` (§4.6) fonte de verdade de relações IFC/espaciais/documentais/museológicas. `relations_summary` em OpenSearch é derivado (só filtros/snippets). Edges com `confidence`+`source`. `MENTIONED_IN` liga elemento↔chunk (expansão de evidência).

**12. Matching visual HBIM↔museu.** `[M8]` Candidatos por `image_embedding_jina_clip_1024` (imagem-imagem e text→image); verificação por VLM; `VISUALLY_MATCHES {score,model,version,evidence}` só acima de limiar e sem conflito de metadados. Nunca match definitivo sem evidência (§7.6).

**13. Router determinístico antes de qualquer fallback para AMALIA.** `[M4]` `router.py` implementa §6 integralmente. AMALIA **não** classifica no caminho principal; entra só para (a) reescrita de follow-up quando necessária, (b) query expansion opcional **após** o router, (c) resposta final grounded (§6 "onde AMALIA ainda entra"/"não deve").

**14. Evidence aggregation e deduplicação.** `[M5]` `evidence.py` agrupa por elemento/documento/página, deduplica por `evidence_id` (hash de source_type+source_id+span), funde evidência textual+grafo+visual, e anexa `caveats` (`missing_geometry`, `inferred_relation`, `visual_match_not_confirmed`).

**15. Respostas grounded com citações.** `[M5]` AMALIA recebe **apenas** o `EvidencePack` + instrução forte (§9): responde só do pack, inclui ids elemento/documento/página, declara falta de evidência quando aplicável, não inventa. `responses.py` valida que toda afirmação factual tem `evidence_id` associável.

**16. Confidence thresholds e abstention.** `[M5,M8]` Limiares por método (reranker, similaridade visual) afinados no gold. Se abaixo do limiar → abstenção explícita ("sem evidência suficiente") em vez de resposta especulativa. Métrica de *abstention correctness* no eval.

**17. Dataset de avaliação e regressão.** `[Transversal]` Gold set por categoria (structured/semantic/historical/spatial/document/visual), `run_eval.py` + `metrics.py` (Recall@k, nDCG@k, MRR, routing accuracy, abstention). CI bloqueia regressões acima de tolerância. Baseline estabelecido em M3; cada milestone valida o seu slice.

---

## 5. Modelos e gestão de GPU — RTX PRO 6000 Blackwell (96 GB GDDR7)

> VRAM **aproximada** (inferência servida, inclui KV/ativações modestos; varia com contexto e batch). Modelos e responsabilidades são `[DOC]`; precisões, footprints, perfis de residência e o VLM concreto são `[REC]` desta análise. **Premissa-chave desta versão:** nem todos os modelos estão residentes ao mesmo tempo — a GPU é gerida por *perfis de residência* (§5.3), de modo a que a soma nunca exceda o orçamento de VRAM.

### 5.1 Tabela de modelos

| Modelo | Responsabilidade | Precisão | VRAM aprox. | Batch inicial | Estratégia de batching | Serviço | Fallback | Métricas |
|---|---|---|---|---|---|---|---|---|
| **Qwen3-Embedding-8B** | Embeddings densos texto (elements, chunks, captions); **dim por índice** (1024/2048/4096, benchmark) | **BF16** (qualidade) | ~18–22 GB | 16 (docs), 1 (query) | Fila dinâmica por tokens; lote grande offline | **vLLM** ou TEI | TEI/Infinity; BGE-M3 | Recall@k, nDCG@k |
| **Qwen3-Reranker-8B** | Rerank cross-encoder top-100–300 | **BF16** (qualidade) | ~18–22 GB | 32 pares | Só se candidatos>K; janela truncada | vLLM/TEI | bge-reranker-v2-m3 | ΔnDCG@10 |
| **jina-clip-v2** | Embeddings multimodais imagem/texto @1024 | FP16 | ~2–3 GB | 32 imagens | Lote 512×512 | vLLM/TEI/próprio | OpenCLIP | Precisão@k visual |
| **PaddleOCR-VL-1.6** (fb 0.9B) | OCR/parse de digitalizações, tabelas, plantas | FP16 | ~3–6 GB | 4 páginas | Página a página; cache por checksum | Serviço PaddleOCR-VL | 0.9B | CER/WER |
| **Docling** | Conversão estruturada (reading order, tabelas) | CPU + layout leve | <2 GB | por documento | Pipeline por documento | Lib/serviço | PyMuPDF+regras | % estrutura |
| **ColQwen2.5** (opcional) | Visual doc retrieval late-interaction (plantas) | BF16 | ~7–9 GB | 8 páginas | Multi-vector; store dedicado se escalar (§2) | vLLM | adiar | Recall visual |
| **Qwen3-VL-8B-Instruct** *(verifier default)* | Verificação visual gated + leitura de página | **FP8** | ~9–11 GB | 1–2 imagens | On-demand; só top-candidatos | vLLM | BF16 ~17–19 GB | FP visual pós-verificação |
| **Qwen3-VL-32B-Instruct** *(verifier escalonado)* | Casos difíceis / OCR-PT exigente | FP8/AWQ | ~34–40 GB | 1 imagem | Janela exclusiva (§5.3) | vLLM | — | idem |

### 5.2 Qual VLM — e porquê (responde diretamente à questão)

O documento pedia `Qwen2.5-VL-72B` "ou o melhor Qwen-VL self-hosted disponível **no momento da implementação**". Esse momento é agora, e o 72B em BF16 (~145 GB) **não cabe** em 96 GB. A escolha atualizada e viável é a família **Qwen3-VL** (mais recente e mais forte que a 2.5), em dois níveis:

- **Default: `Qwen3-VL-8B-Instruct` em FP8 (~9–11 GB).** Existe checkpoint FP8 oficial com qualidade praticamente idêntica ao BF16. É pequeno o suficiente para ficar **sempre co-residente** com os embedders e o reranker. Resolve a esmagadora maioria das verificações.
- **Escalonamento: `Qwen3-VL-32B-Instruct` em FP8/AWQ (~34–40 GB).** Só é chamado quando o 8B sinaliza incerteza ou quando a leitura exige OCR-PT de alta fidelidade (o 32B é notoriamente melhor em OCR não-inglês). Corre numa **janela exclusiva** (§5.3), nunca em simultâneo com o stack de retrieval em BF16.
- **Fora de alcance para esta GPU:** 72B (mesmo FP8 ~75–80 GB exige a placa quase inteira, sem folga para co-residência) e o MoE 235B-A22B. Não são recomendados aqui.

**E — do turno anterior — o VLM NÃO é um retriever.** O retrieval e o reranking visual fazem-se com *embedders* (jina-clip) e, se necessário, *late-interaction* (ColQwen). O VLM entra **depois**, e só em dois trabalhos: (a) **verificar** se um candidato visual é realmente correspondência antes de escrever uma aresta `VISUALLY_MATCHES` no grafo, e (b) **ler** uma imagem/página para grounding ("onde está a entrada nesta planta?"). Além disso está **gated** atrás de um filtro determinístico de metadados (material/período/dimensões) — só corre nos poucos candidatos que sobrevivem ao gate. Consequência prática: o VLM é invocado poucas vezes por sessão, pelo que a janela cara do 32B é rara e agendável.

### 5.3 Gestão de residência de VRAM (o núcleo do pedido)

**Orçamento.** 96 GB − reserva (~10 GB: contexto CUDA, fragmentação, burst de KV) = **~86 GB utilizáveis**. Nenhum perfil pode exceder isto.

**Princípio.** Prioridade de qualidade → **embedder e reranker ficam em BF16** (são o coração do retrieval). Tudo o resto (VLM, jina, OCR) em **FP8/FP16**, que a Blackwell corre nativamente. Cada modelo é um **serviço isolado** (vLLM/TEI) com `--gpu-memory-utilization` fixo; a API chama por HTTP (elimina o carregamento in-process duplicado de §1.4/§1.12). Um **gestor de residência** garante o perfil-alvo ativo antes de despachar.

**Perfis de residência** (soma sempre ≤ ~86 GB):

| Perfil | Modelos residentes | Soma aprox. | Quando |
|---|---|---|---|
| **P-Online-Text** | Emb-8B (BF16) + Rerank-8B (BF16) | ~40 GB | Queries texto/estruturado/híbrido (a maioria) |
| **P-Online-MM** | P-Online-Text + jina-clip + OCR + **Qwen3-VL-8B (FP8)** | ~58 GB | Queries multimodais/documentais; verificação leve |
| **P-Verify-Hard** *(exclusivo)* | **Qwen3-VL-32B (FP8)** (Emb/Rerank em *sleep*) | ~38 GB | Escalonamento raro do verifier / OCR-PT difícil |
| **P-Ingest-Docs** | OCR + Docling + Emb-8B | ~27 GB | Ingestão offline de PDFs (não concorrente com online pesado) |
| **P-Ingest-Visual** | jina-clip + ColQwen + Emb-8B | ~33 GB | Indexação multimodal offline |

**Aritmética de segurança (para validar em implementação).** `P-Online-MM` = 20+20+3+5+10 = **~58 GB** ⇒ folga ~28 GB para KV/burst. A janela `P-Verify-Hard` **não** soma com Emb+Rerank: a verificação é pós-retrieval e *gated*, logo o retrieval está ocioso nesse instante — o gestor coloca Emb+Rerank em *sleep* (liberta ~40 GB), sobe o 32B (~38 GB) e depois acorda-os. Assim nunca há dois picos em simultâneo.

**Componente a construir `[REC/novo]` — gestor de residência.** `models/residency.py` (+ endpoint de ops) com:
- registo `{modelo → {estado: loaded|sleeping|unloaded, vram_medida}}`;
- invariante `Σ vram_residente ≤ VRAM_BUDGET_GB` (env), recusa/adiando carregamentos que a violem;
- `ensure_profile(profile)` que carrega/eviction/`sleep`/`wake` para atingir o perfil-alvo (usar **vLLM sleep mode**: nível 1 offload de KV, nível 2 offload de pesos — liberta VRAM sem matar o processo);
- **lock/semáforo** para a janela exclusiva do 32B (serializa verificações difíceis);
- o **router determinístico** (M4) já sabe a rota → sabe o perfil necessário e pede `ensure_profile` antes de despachar.

**Config sugerida (env).**
```env
VRAM_BUDGET_GB=86
EMBED_DTYPE=bfloat16
RERANK_DTYPE=bfloat16
VLM_MODEL=Qwen/Qwen3-VL-8B-Instruct-FP8
VLM_ESCALATION_MODEL=Qwen/Qwen3-VL-32B-Instruct-FP8
VLM_ESCALATION_EXCLUSIVE=true      # sleep de Emb/Rerank durante a janela
GPU_MEMORY_UTILIZATION_EMBED=0.25
GPU_MEMORY_UTILIZATION_RERANK=0.25
GPU_MEMORY_UTILIZATION_VLM8B=0.14
```

**Caminho mais simples (alternativa `[REC]`).** Se preferires evitar *sleep/wake*: quantizar **também** Emb+Rerank em FP8 (~11 GB cada). Online: 11+11+3+5+10 = ~40 GB, e cabe **ainda** o 32B FP8 (~38) → ~78 GB, tudo co-residente, **sem eviction**. Custo: ligeira perda de qualidade de retrieval (aceitável só se a avaliação §17 não regredir além da tolerância). Default recomendado continua a ser BF16 no retrieval + perfis, por o projeto priorizar qualidade.

**Regra de ouro (do projeto).** Nenhum modelo entra em routing, parsing, filtragem, agregação ou operações determinísticas. VLM/LLM só em: resposta grounded, verificação visual *gated* de candidatos finais, leitura de imagem/página, e sugestão de candidatos em entity linking **não resolvido** por regras.

---

## 6. Backlog ordenado (issues executáveis)

Ordenado por dependência e prioridade. Prioridade: **P0** (bloqueante/segurança), **P1** (fundação), **P2** (funcionalidade central), **P3** (avançado). Complexidade S/M/L/XL.

### HBIM-001 — Rodar e remover password OpenSearch hardcoded — **P0 / S**
- **Descrição.** `config.py` tem `OPENSEARCH_PASSWORD` default `<redigido>`. Rodar a credencial no cluster e remover o default; arranque falha se ausente.
- **Ficheiros.** `shared/config.py`.
- **Dependências.** —
- **Aceitação.** `git grep` sem segredos; app não arranca sem env; credencial antiga revogada.

### HBIM-002 — Config tipada segmentada + ligação OpenSearch + bootstrap de pytest — **P0 / M**
- **Descrição.** Settings Pydantic **segmentadas por consumidor** (`OpenSearchSettings`/`ApiSettings`/`LlmSettings`), `SecretStr`. **`OPENSEARCH_VERIFY_CERTS` default `true`** (correção 1); um ambiente específico pode pôr `false` explícito. **SSL inferido só do `scheme`** quando `OPENSEARCH_USE_SSL` não definido — **nunca** das credenciais (correção 2). Aceitar aliases (`OPENSEARCH_USER`/`USERNAME`, `USE_SSL`/`OPENSEARCH_USE_SSL`, …); **normalizar host-com-esquema** (forma de URL ou separar scheme/host — não passar `https://…` em `{"host":…}`). **Extractor IFC não instancia `OpenSearchSettings`** e corre sem password/cliente (correção 3). **Nenhum cliente de rede criado no import** — factories *lazy* (correção 4). **Bootstrap mínimo de pytest + `test_config`** (correção 5). Todos os valores no repo são fictícios; password vazia.
- **Ficheiros.** `shared/config.py`, `shared/opensearch.py`, `api/search.py` (remover clientes a nível de módulo), `.env.example`, `backend/tests/test_config.py`, `backend/pytest.ini`, `backend/tests/conftest.py`.
- **Dependências.** HBIM-001.
- **Aceitação.** `pytest -q` corre localmente; `test_config` cobre: falha sem password; `verify_certs` default `True`; `use_ssl` inferido só do scheme (http+credenciais ⇒ SSL off); extractor importa e corre **sem env OpenSearch**; importar `api.search` **não** abre ligações; host-com-esquema não parte o cliente.

### HBIM-003A — Endurecer API (backend): auth + CORS + healthchecks + logging — **P0 / M**
- **Descrição.** Auth (API key/JWT) no `/chat`; CORS restrito por env; `/healthz`/`/readyz`; logging JSON + `request_id`; `/metrics`.
- **Ficheiros.** `api/main.py`, `shared/security.py`, `shared/logging.py`.
- **Dependências.** HBIM-002.
- **Aceitação.** `/chat` 401 sem chave; CORS sem `*`+credentials; métricas expostas.

### HBIM-003B — Integração de auth no frontend — **P0 / S**
- **Descrição.** O frontend React (`frontend/src`) passa a enviar a credencial (header `Authorization`/API key) e a tratar `401`/expiração. **Entregar em conjunto com 003A** (ou imediatamente a seguir) para não deixar a app partida (correção 10).
- **Ficheiros.** `frontend/src/*` (cliente de API), `.env` do frontend.
- **Dependências.** HBIM-003A.
- **Aceitação.** Frontend autentica e continua funcional após ativar auth; sem chamadas anónimas ao `/chat`.

### HBIM-004 — CI, lint/type-check, testcontainers e compose — **P0 / M**
- **Descrição.** CI (correr `pytest`), `ruff`, `mypy`, **testcontainers** (OpenSearch+Neo4j) e `docker-compose.dev.yml`. *(O bootstrap d    e pytest e `test_config` já vêm de HBIM-002 — correção 5.)*
- **Ficheiros.** `docker-compose.dev.yml`, config CI, `pyproject.toml`/`ruff.toml`/`mypy.ini`.
- **Dependências.** HBIM-002.
- **Aceitação.** CI verde; `ruff`+`mypy` a correr; testcontainers OpenSearch disponível para testes de integração.

### HBIM-005 — Evaluation harness + dataset inicial + baseline do sistema atual — **P1 / M**
- **Descrição.** Criar o *evaluation harness* (`eval/run_eval.py`, `eval/metrics.py`: Recall@k, nDCG@k, MRR, routing accuracy, abstention), um **dataset gold inicial** por categoria (structured/semantic/historical/spatial/document/visual, começando pelas categorias já suportadas) e medir a **baseline do sistema atual** (pipeline zembed + `bim_elements` + filtro-LLM). **Tem de existir antes de qualquer alteração de retrieval** (M4/M5) para se poder medir regressão/ganho (correção 7). O benchmark de dimensão (HBIM-031) e todas as comparações posteriores usam este harness.
- **Ficheiros.** `eval/run_eval.py`, `eval/metrics.py`, `eval/dataset/*_gold.jsonl`, `eval/baselines/current_system.json`.
- **Dependências.** HBIM-002, HBIM-004.
- **Limite explícito.** A categoria `semantic_vector` mede o **caminho kNN** com vetores sintéticos desenhados à mão, não a qualidade do modelo de embeddings (spec §95/§302). A avaliação de qualidade de modelo é criada em **HBIM-005B**.
- **Aceitação.** Harness corre end-to-end contra o sistema atual e grava métricas versionadas; baseline reproduzível; dataset inicial com ≥ N queries por categoria suportada.

### HBIM-010 — Schema canónico Pydantic (IR) — **P1 / L**
- **Descrição.** `ElementRecord/PropertyFact/DocumentRef/ClassificationFact/...`, versionado, JSONL.
- **Ficheiros.** `canonical/schema.py`, `tests/test_canonical_schema.py`.
- **Dependências.** HBIM-004.
- **Aceitação.** Valida 100% do IFC de amostra; `ElementRecord` sem campos dinâmicos.

### HBIM-011 — Refatorar extractor para records canónicos + ler IfcSpace — **P1 / L**
- **Descrição.** `extract_bim.py`→`ifc_extractor.py` emitindo streams tipados; capturar `IfcSpace` (space_id/name).
- **Ficheiros.** `ingestion/ifc_extractor.py` (de `extract_bim.py`), `ingestion/normalize.py`.
- **Dependências.** HBIM-010.
- **Aceitação.** Emite elements/facts/documents separados; espaços populados; golden JSONL estável; **corre sem qualquer variável OpenSearch definida** (correção 3).

### HBIM-012 — Property fact extraction + dedup — **P1 / M**
- **Descrição.** psets/qtos → `PropertyFact` atómicos com `value_type`/`unit`; dedup por `fact_id`.
- **Ficheiros.** `ingestion/ifc_extractor.py`, `tests/test_property_facts.py`.
- **Dependências.** HBIM-011.
- **Aceitação.** 0 propriedades dinâmicas no elemento; factos raros pesquisáveis.

### HBIM-020 — Mappings estáticos dos índices (elements/facts/chunks/documents) — **P1 / L**
- **Descrição.** JSON de mapping `dynamic:strict`; sem `properties`/`quantities` dinâmicos; `classification_codes` keyword.
- **Ficheiros.** `canonical/mappings/*.json`, `ingestion/indexers/*`.
- **Dependências.** HBIM-010.
- **Aceitação.** `test_index_mappings`; indexar 3 IFC não aumenta `total_fields` de facts.

### HBIM-021 — Migração por alias + fim da recriação destrutiva — **P1 / M**
- **Descrição.** `migrate.py` cria `*_vN`, indexa, promove alias; remove `delete+create` de `create_index`.
- **Ficheiros.** `ingestion/index_to_opensearch.py`, `ingestion/migrate.py`.
- **Dependências.** HBIM-020.
- **Aceitação.** Swap de alias sem downtime demonstrado; rollback por realias.

### HBIM-022 — Indexers separados por índice — **P1 / M**
- **Descrição.** `elements/property_facts/documents/chunks` indexers a partir do JSONL canónico.
- **Ficheiros.** `ingestion/indexers/*_indexer.py`.
- **Dependências.** HBIM-021.
- **Aceitação.** Round-trip por `_id`; contagens corretas.

### HBIM-030 — Serviço de embeddings Qwen3-Embedding-8B (vLLM/TEI) — **P1 / L**
- **Descrição.** Servir o modelo; cliente `embeddings_qwen3.py` com dimensões {1024,2048,4096} (Matryoshka); remover `SentenceTransformer` in-process, `_validate_embedding_dim` e `SUPPORTED_EMBEDDING_DIMS` (validação por-modelo). Estabelece já a **convenção de serviço isolado** que o gestor de residência (HBIM-032) vai orquestrar.
- **Ficheiros.** `models/embeddings_qwen3.py`, `deploy/embeddings.*`, `ingestion/indexers/*`, `api/search.py`.
- **Dependências.** HBIM-022.
- **Aceitação.** Cliente devolve as três dimensões-alvo; API/indexer sem modelo in-process; latência p95 por dimensão registada.

### HBIM-005B — Gold semântico pré-registado + baseline de qualidade de modelo — **P1 / M**
- **Descrição.** Criar o pré-requisito de avaliação semântica que faltava: corpus canónico sintético (≥120 `ElementRecord`), necessidades de informação em linguagem natural PT/EN, julgamentos de relevância **graduados e derivados** por função pura, projeção de texto versionada e um **commit de pré-registo** anterior a qualquer inferência. Depois mede a **qualidade do modelo legado `zeroentropy/zembed-1`@640** e uma **referência `Qwen3-Embedding-8B`@4096** por cosseno exato. Não escolhe dimensão de produção nem cria índice denso.
- **Ficheiros.** `eval/semantic_gold/*`, `eval/semantic_gold_dataset.py`, `eval/text_projection.py`, `eval/models/*`, `eval/run_semantic_baseline.py`, `eval/baselines/semantic_model_quality.json`.
- **Dependências.** HBIM-005, HBIM-010/022, HBIM-030.
- **Aceitação.** Gold pré-registado e imutável (hashes verificados antes de qualquer modelo); Recall@10/nDCG@10/MRR@10 medidos para ambos os modelos; artefacto canónico sem vetores nem identificadores.

### HBIM-031 — Benchmark de dimensão por índice + reindex denso — **P1 / M**
- **Descrição.** Correr o benchmark 1024/2048/4096 por índice usando o harness de HBIM-005 e o **gold semântico de HBIM-005B** (nDCG/Recall × tamanho × latência); **selecionar e aplicar a dimensão por índice** (correção 9); reindexar `elements`/`chunks` com a dimensão vencedora. A seleção de 1024/2048/4096, o campo vetorial na nova versão de mapping e o reindex denso continuam a pertencer **exclusivamente** a esta issue.
- **Ficheiros.** `eval/dim_benchmark.py`, `ingestion/migrate.py`, `canonical/mappings/*.json`.
- **Dependências.** HBIM-030, HBIM-005, **HBIM-005B** (baseline de qualidade de modelo).
- **Aceitação.** kNN funcional; **dimensão documentada e aplicada por índice** com números; Recall@10 ≥ à baseline zembed registada em `eval/baselines/semantic_model_quality.json` (HBIM-005B). 

### HBIM-032 — Gestor de residência de VRAM + perfis de GPU — **P2 / L**
- **Descrição.** `models/residency.py` + endpoint de ops. Registo de modelos carregados com VRAM medida; invariante `Σ ≤ VRAM_BUDGET_GB`; `ensure_profile()` com load/evict/`sleep`/`wake` (vLLM sleep mode); lock para a janela exclusiva do VLM-32B. Perfis `P-Online-Text`, `P-Online-MM`, `P-Verify-Hard`, `P-Ingest-Docs`, `P-Ingest-Visual` (§5.3). Router pede o perfil antes de despachar. **O gestor completo só faz sentido depois de o reranker estar servido** (perfil `P-Online-Text` = embedder+reranker), pelo que depende de HBIM-051 (correção 6). Até lá, HBIM-030 já garante serviços isolados suficientes para o caminho denso.
- **Ficheiros.** `models/residency.py`, `retrieval/router.py`, `deploy/*` (serviços vLLM/TEI isolados), `shared/config.py` (budget/utilizações).
- **Dependências.** HBIM-030 **e HBIM-051** (reranker servido — pré-requisito do gestor completo).
- **Aceitação.** Teste que simula ativação de perfis nunca excede `VRAM_BUDGET_GB`; janela `P-Verify-Hard` coloca Emb/Rerank em *sleep* e recupera; VLM só carrega on-demand.

### HBIM-040 — Router determinístico (§6) — **P2 / L**
- **Descrição.** `router.py` com todas as regras/termos §6; remove classificação por LLM.
- **Ficheiros.** `retrieval/router.py`, `api/main.py`, `tests/test_router.py`.
- **Dependências.** HBIM-022, **HBIM-005** (harness + `routing_gold` têm de existir antes de mexer no retrieval — correção 7).
- **Aceitação.** Routing accuracy ≥95% no `routing_gold`; 0 LLM no routing.

### HBIM-041 — Query parser determinístico — **P2 / L**
- **Descrição.** Regex+dicionários p/ ifc_class, material, storey, condições, GlobalId, agg_field; deprecar prompts de extração.
- **Ficheiros.** `retrieval/query_parser.py`, `api/prompts.py`, `tests/test_query_parser.py`.
- **Dependências.** HBIM-040.
- **Aceitação.** Paridade/melhoria vs extração LLM no gold; 0 LLM no parsing.

### HBIM-042 — Aplicar filtros material/storey/name + corrigir agregação de classificação — **P2 / M**
- **Descrição.** `lexical.py`/`build_opensearch_query` passam a filtrar material/storey/name; agregação usa `classification_codes` keyword (corrige bug nested/text).
- **Ficheiros.** `retrieval/lexical.py`, `api/search.py`.
- **Dependências.** HBIM-041.
- **Aceitação.** "paredes de pedra no piso 1" filtra pedra+piso1; agregação de classificação devolve buckets corretos.

### HBIM-050 — BM25 + dense + RRF (hybrid) — **P2 / L**
- **Descrição.** `dense.py`, `lexical.py` (BM25), `rrf.py`, `hybrid.py` (top-200+top-200→RRF).
- **Ficheiros.** `retrieval/{dense,lexical,rrf,hybrid}.py`, `tests/test_rrf.py`.
- **Dependências.** HBIM-030, HBIM-040.
- **Aceitação.** RRF determinístico; preservação da união de candidatos (BM25∪dense, sem perda de fonte); paridade de IDs e filtros canónicos entre as duas fontes; comparação diagnóstica reproduzível BM25 vs dense vs RRF-cru. O nDCG@10 do RRF-cru é **diagnóstico** (pode ficar abaixo do dense na saturação corpus<200); o gate bloqueante nDCG@10 ≥ dense-sozinho pertence a HBIM-051 após reranking (M5 l.6 `… → RRF → reranker`; M5 l.42).

### HBIM-051 — Qwen3-Reranker-8B + remover FILTER_RESULTS_BATCH — **P2 / L**
- **Descrição.** `rerank.py` + serviço; remover pós-filtro por LLM; limiares afinados.
- **Ficheiros.** `retrieval/rerank.py`, `models/reranker_qwen3.py`, `api/main.py`, `api/prompts.py`.
- **Dependências.** HBIM-050.
- **Aceitação.** `FILTER_RESULTS_BATCH` ausente; **nDCG@10 do hybrid reranked ≥ dense-sozinho** no gold (ΔnDCG@10 positivo); recall não desce vs baseline LLM-filter.

### HBIM-052 — EvidencePack + dedup + agregação — **P2 / L**
- **Descrição.** `evidence.py` monta `EvidencePack` (dedup, agrupamento, caveats).
- **Ficheiros.** `retrieval/evidence.py`, `api/schemas.py`, `tests/test_evidence_pack.py`.
- **Dependências.** HBIM-051.
- **Aceitação.** Nenhum item sem `source_id`; scores/métodos no output.

### HBIM-053 — AMALIA grounded + citações + abstenção — **P2 / M**
- **Descrição.** Prompt §9; `responses.py` valida citações; abstenção abaixo de limiar.
- **Ficheiros.** `api/responses.py`, `api/prompts.py`.
- **Dependências.** HBIM-052.
- **Aceitação.** Facto ausente do pack → "sem evidência suficiente"; ids presentes quando existem.

### HBIM-060 — Expandir harness + regression gates em CI — **P2 / M (contínuo)**
- **Descrição.** **Expandir** o harness/dataset criado em HBIM-005 (mais categorias e queries à medida que os milestones avançam) e **aplicar *regression gates*** no CI: bloquear merges que baixem nDCG/Recall/routing-accuracy além da tolerância face à baseline (correção 8). Não cria a avaliação — essa é HBIM-005.
- **Ficheiros.** `eval/dataset/*_gold.jsonl` (expansão), `eval/run_eval.py` (gates), config CI.
- **Dependências.** HBIM-005 (harness+baseline); cresce com M4→M8.
- **Aceitação.** CI falha em regressão > tolerância; cobertura de categorias acompanha os milestones entregues.

### HBIM-070 — Ingestão de documentos (Docling) + chunking — **P2 / XL**
- **Descrição.** `document_ingestor.py`, `chunking.py`; `documents` deixa de ser `enabled:false`.
- **Ficheiros.** `ingestion/document_ingestor.py`, `ingestion/chunking.py`, `indexers/{chunks,documents}_indexer.py`.
- **Dependências.** HBIM-022, HBIM-030.
- **Aceitação.** PDF→chunks pesquisáveis com página/secção.

### HBIM-071 — OCR PaddleOCR-VL + bboxes + rasterização — **P2 / L**
- **Descrição.** OCR de digitalizações; `bbox_on_page`; páginas rasterizadas p/ media.
- **Ficheiros.** `models/ocr_paddle.py`, `ingestion/document_ingestor.py`.
- **Dependências.** HBIM-070.
- **Aceitação.** Digitalizações pesquisáveis; bbox válido.

### HBIM-072 — Entity linking chunk→elemento — **P2 / L**
- **Descrição.** GlobalId/nome/localização + fuzzy determinístico; LLM só p/ não resolvidos.
- **Ficheiros.** `ingestion/entity_linking.py`, `tests/test_entity_linking.py`.
- **Dependências.** HBIM-070.
- **Aceitação.** % de ligação ≥ meta no `document_gold`.

### HBIM-073 — Hybrid retrieval documental integrado no EvidencePack — **P2 / M**
- **Descrição.** Rota `document_hybrid` sobre `chunks_v1`.
- **Ficheiros.** `retrieval/hybrid.py`, `retrieval/router.py`.
- **Dependências.** HBIM-052, HBIM-072.
- **Aceitação.** Citação documento+página+chunk na resposta.

### HBIM-080 — Extração geométrica (bbox/centróide/orientação) — **P3 / XL**
- **Descrição.** `geometry_extractor.py` com ifcopenshell geom + numpy; `geometry` em elements.
- **Ficheiros.** `ingestion/geometry_extractor.py`, `indexers/elements_indexer.py`, `tests/test_geometry.py`.
- **Dependências.** HBIM-011.
- **Aceitação.** % elementos com bbox ≥ meta; valores corretos p/ sólidos conhecidos.

### HBIM-081 — Relações espaciais derivadas + IFC nativas — **P3 / L**
- **Descrição.** `spatial_relations.py`: containment/adjacency/above-below/intersects; edges com `confidence/source`.
- **Ficheiros.** `ingestion/spatial_relations.py`, `tests/test_spatial_relations.py`.
- **Dependências.** HBIM-080.
- **Aceitação.** Relações IFC nativas + ≥1 derivada; precisão no `spatial_gold`.

### HBIM-082 — Neo4j KG + graph retrieval (Cypher) — **P3 / XL**
- **Descrição.** `kg_builder.py`, `shared/neo4j.py`, `retrieval/graph.py`; `relations_summary` derivado.
- **Ficheiros.** `ingestion/kg_builder.py`, `shared/neo4j.py`, `retrieval/graph.py`, `tests/test_graph_retrieval.py`.
- **Dependências.** HBIM-081, HBIM-052.
- **Aceitação.** Rota `graph` devolve `graph_paths`; Neo4j é fonte de verdade.

### HBIM-090 — Índice media + jina-clip-v2 + ingestão de imagens/museu — **P3 / XL**
- **Descrição.** `hbim_media_v1`; `image_ingestor.py`/`museum_ingestor.py` (CIDOC-lite); embeddings visuais.
- **Ficheiros.** `ingestion/{image,museum}_ingestor.py`, `indexers/media_indexer.py`, `models/jina_clip.py`.
- **Dependências.** HBIM-022, HBIM-030.
- **Aceitação.** text→image e image→image funcionais; media pesquisável.

### HBIM-091 — Gate de metadados + VLM verifier (Qwen3-VL) + VISUALLY_MATCHES — **P3 / XL**
- **Descrição.** Retrieval/ranking visual por embedders (jina-clip/ColQwen), **gate determinístico de metadados** (`metadata_gate.py`), e só então **VLM `Qwen3-VL-8B` FP8** (escalonar a 32B via `P-Verify-Hard`). `VISUALLY_MATCHES` só acima de limiar/sem conflito. VLM **fora** do caminho de retrieval e carregado on-demand pelo gestor de residência.
- **Ficheiros.** `retrieval/multimodal.py`, `retrieval/metadata_gate.py`, `models/vlm_verifier.py`, `ingestion/kg_builder.py`, `tests/test_multimodal.py`, `tests/test_metadata_gate.py`.
- **Dependências.** HBIM-090, HBIM-082, HBIM-052, HBIM-032.
- **Aceitação.** 0 chamadas VLM antes do gate; nenhum match sem score/modelo/limiar/evidência; escalonamento 32B respeita orçamento de VRAM; caveats no pack.

### HBIM-092 — ColQwen visual document retrieval (opcional) — **P3 / L**
- **Descrição.** Late-interaction p/ plantas/layout; storage multi-vector dedicado se escalar (§2).
- **Ficheiros.** `retrieval/multimodal.py`, `models/colqwen.py`.
- **Dependências.** HBIM-091.
- **Aceitação.** Recall visual de página em queries dependentes de layout.

---

## 7. Sequência recomendada de execução

1. **Segurança/base:** HBIM-001 → 002 → 003A+003B → 004.
2. **Avaliação primeiro:** **HBIM-005** (harness + dataset + baseline do sistema atual) — antes de qualquer alteração de retrieval (correção 7).
3. **Dados canónicos + índices + denso:** HBIM-010→012, 020→022, 030, **031** (benchmark de dimensão por índice, usa HBIM-005).
4. **Determinismo + hybrid + grounding:** HBIM-040→042, 050 → **051** → **032** (gestor de residência completo, só após 051 — correção 6) → 052→053. *(regression gates de HBIM-060 acompanham este bloco.)*
5. **Documentos:** HBIM-070→073.
6. **Grafo/geometria:** HBIM-080→082.
7. **Multimodal/museu:** HBIM-090→092.

Cada bloco deixa o sistema funcional e avaliável. Nenhuma reescrita total: `extract_bim.py`, `search.py`, `index_to_opensearch.py`, `main.py` evoluem por extração de módulos, não por substituição em bloco. As decisões-mãe (OpenSearch+Neo4j, Qwen3 embedding/reranker, router determinístico, índices separados, EvidencePack, AMALIA grounded, multimodal com verificação) mantêm-se **intactas**; a v3 só corrige configuração, segurança, ordem de avaliação, dependências e a fixação prematura da dimensão de embedding.

## Future product direction

A future version may support user-submitted IFC files, with local or
server-side ingestion and indexing managed entirely by the application.

This may include:

- project lifecycle management;
- upload-triggered ingestion jobs;
- progress and failure recovery;
- isolation by `project_id`;
- automatic local service management;
- desktop packaging.

This is not part of the current implementation scope.