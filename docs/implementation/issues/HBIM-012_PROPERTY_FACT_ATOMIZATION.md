# HBIM-012 — Atomização e deduplicação de `PropertyFact`

> **Tipo:** especificação executável de issue.  
> **Branch obrigatória:** `feat/hbim-012-property-fact-atomization`.  
> **Depende de:** HBIM-010 (schema canónico) e HBIM-011 (extração IFC → canónico), ambas merged.

---

## 0. Precedência e fonte de verdade

HBIM-010 e HBIM-011, já implementadas e merged, são a fonte de verdade e são
**imutáveis** nesta issue para:

- `PropertyFact` v1.0;
- `property_fact_id`;
- `schema_version = "1.0"`;
- serialização canónica;
- APIs públicas `convert_ifc_to_canonical` e `write_canonical_jsonl`;
- escrita atómica por diretório;
- contratos de warnings e coverage;
- import-safety.

**`backend/canonical/**` não é alterado nesta issue.**

O resultado continua a ser `PropertyFact` v1.0. Qualquer necessidade futura de:

- origem `instance`/`type` persistida;
- unidade original e normalizada simultaneamente;
- valor normalizado;
- referência IFC tipada como valor;

exige schema canónico 1.1 e issue/ADR separada.

---

## 1. Objetivo

Substituir o produtor interno de `PropertyFact`, atualmente baseado em
`ifcopenshell.util.element.get_psets`, por um traversal IFC raw que preserve
estrutura suficiente para produzir factos escalares determinísticos:

```text
propriedade IFC
→ leitura raw
→ classificação do tipo IFC
→ validação estrutural
→ precedência instance/type
→ atomização
→ deduplicação/conflitos
→ PropertyFact v1.0
→ warnings e coverage determinísticos
```

Esta issue cobre:

- `IfcPropertySingleValue`;
- `IfcPropertyEnumeratedValue`;
- `IfcPropertyListValue`;
- `IfcPropertyBoundedValue`;
- `IfcPropertyTableValue`;
- `IfcPropertyReferenceValue`;
- `IfcComplexProperty`;
- quantidades físicas simples;
- `IfcPhysicalComplexQuantity`;
- propriedades de instância e de tipo;
- unidades;
- duplicados;
- overrides;
- conflitos;
- limites contra explosão de factos.

### 1.1 Porquê traversal raw

A auditoria da HBIM-012 confirmou que `get_psets` perde informação necessária:

- omite silenciosamente `IfcPropertyReferenceValue`;
- devolve bounded/table como estruturas parcialmente wrapped;
- torna enum indistinguível de list;
- funde instância e tipo, ocultando a origem e o valor substituído;
- não preserva unidades por papel/célula;
- obriga a HBIM-011 a usar leitores paralelos para valores e unidades.

A HBIM-012 deixa de usar `get_psets` como produtor de `PropertyFact`.

---

## 2. Decisões ratificadas

### 2.1 Schema v1.0 inalterado

- `origin` não é persistida em `PropertyFact`;
- instância tem precedência sobre tipo;
- overrides e proveniência entram apenas em diagnostics/coverage;
- `unit` guarda um único rótulo efetivo;
- não existem `unit_norm` ou `value_norm`;
- referências IFC não geram `PropertyFact`;
- referências entram como `unsupported_v1`;
- normalização semântica fica fora de scope.

### 2.2 Scalar parity obrigatória

Para `IfcPropertySingleValue` e quantidades simples:

```text
occurrence_key = "0"
```

Isto preserva os `fact_id` produzidos pela HBIM-011. Nunca substituir por
`"single"` ou `"quantity"`.

### 2.3 Listas e enumerações

Listas e enumerações são sequências ordenadas:

```text
item:000000
item:000001
...
```

A reordenação altera os IDs e é considerada uma alteração estrutural
intencional. Itens repetidos permanecem ocorrências distintas por índice.

### 2.4 Referências IFC

`IfcPropertyReferenceValue`:

- nunca é convertido por `str()`;
- nunca é convertido para `TEXT`;
- não produz `PropertyFact` em v1.0;
- produz diagnostic e coverage `unsupported_v1`.

### 2.5 Override de propriedades compostas

A precedência entre instância e tipo ocorre **antes da atomização**, ao nível da
propriedade completa:

```text
(source, container, property_name)
```

Se a instância definir a propriedade, a propriedade homónima do tipo é
suprimida integralmente. Isto aplica-se também a listas, enums, bounded, tables,
`IfcComplexProperty` e `IfcPhysicalComplexQuantity`.

Nunca se misturam posições da instância com posições residuais do tipo.

---

## 3. Arquitetura e direção de dependências

Dois módulos novos:

| Módulo | Dependências | Responsabilidade |
|---|---|---|
| `backend/ingestion/property_facts.py` | `backend/canonical` e biblioteca standard; **sem IfcOpenShell** | Modelos internos puros, atomização, grammar de `occurrence_key`, precedência, dedup, conflitos, limites e resultados tipados. |
| `backend/ingestion/ifc_properties.py` | IfcOpenShell + `property_facts.py` | Traversal raw de propriedades/quantidades IFC, resolução de unidades, ciclos, dimensão e construção dos modelos internos puros. |

Direção obrigatória:

```text
ifc_properties.py → property_facts.py → backend/canonical
```

Proibido:

```text
property_facts.py → ifc_properties.py
```

Isto garante que `property_facts.py` é importável e testável sem IfcOpenShell.

`canonical_ifc.py`:

- chama `ifc_properties.py`;
- chama a API pura de `property_facts.py`;
- converte diagnostics internos em `ExtractionWarning`;
- integra deltas de coverage;
- mantém as APIs públicas e o writer atómico inalterados.

As métricas HBIM-011 podem continuar temporariamente com `get_psets`, apenas
como caminho heurístico independente. `get_psets` nunca volta a produzir
`PropertyFact`.

---

## 4. Modelos internos puros

Os modelos abaixo vivem em `property_facts.py`.

São proibidos:

- `dict[str, Any]`;
- `tuple[Any, ...]`;
- payloads livres;
- entidades IfcOpenShell;
- `str(entity)`.

### 4.1 Enums fechados

```python
class PropertyOrigin(str, Enum):
    INSTANCE = "instance"
    TYPE = "type"

class PropertySource(str, Enum):
    PSET = "pset"
    QTO = "qto"

class IfcPropertyKind(str, Enum):
    SINGLE = "single"
    ENUMERATED = "enumerated"
    LIST = "list"
    BOUNDED = "bounded"
    TABLE = "table"
    REFERENCE = "reference"
    COMPLEX = "complex"
    SIMPLE_QUANTITY = "simple_quantity"
    COMPLEX_QUANTITY = "complex_quantity"

class ReferenceIdentityKind(str, Enum):
    GLOBAL_ID = "global_id"
    ENTITY_WITHOUT_GLOBAL_ID = "entity_without_global_id"
    UNSUPPORTED_ENTITY = "unsupported_entity"
    NULL_REFERENCE = "null_reference"

class UnitOrigin(str, Enum):
    EXPLICIT_PROPERTY = "explicit_property"
    EXPLICIT_QUANTITY = "explicit_quantity"
    TYPE = "type"
    PROJECT = "project"
    NONE = "none"

class UnitDimension(str, Enum):
    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    MASS = "mass"
    TIME = "time"
    COUNT = "count"
    UNKNOWN = "unknown"

class UnitStatus(str, Enum):
    RESOLVED = "resolved"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"
```

### 4.2 Unidade

```python
@dataclass(frozen=True, slots=True)
class UnitResolution:
    label: str | None
    origin: UnitOrigin
    dimension: UnitDimension | None
    status: UnitStatus
```

Nenhuma entidade IFC fica guardada em `UnitResolution`.

### 4.3 Escalares internos

```text
InternalScalar :=
    IntScalar
  | FloatScalar
  | TextScalar
  | BoolScalar
  | NullScalar
```

`UnsupportedScalar` não existe. Estruturas não suportadas têm uma variante raw
própria e produzem diagnostics/coverage, não valores escalares falsos.

Cada wrapper é uma dataclass frozen com campo `value`, exceto `NullScalar`.

### 4.4 União discriminada de propriedades raw

```text
RawOccurrence :=
    RawSingleOccurrence
  | RawEnumeratedOccurrence
  | RawListOccurrence
  | RawBoundedOccurrence
  | RawTableOccurrence
  | RawReferenceOccurrence
  | RawComplexOccurrence
  | RawSimpleQuantityOccurrence
  | RawComplexQuantityOccurrence
```

Campos comuns:

```python
origin: PropertyOrigin
source: PropertySource
container: str
property_name: str
structural_path: tuple[str, ...]
unit: UnitResolution
ifc_kind: IfcPropertyKind
```

Payloads:

```python
RawSingleOccurrence.value: InternalScalar

RawEnumeratedOccurrence.items: tuple[InternalScalar, ...]

RawListOccurrence.items: tuple[InternalScalar, ...]

RawBoundedOccurrence.lower: InternalScalar | None
RawBoundedOccurrence.upper: InternalScalar | None
RawBoundedOccurrence.setpoint: InternalScalar | None

RawTableOccurrence.rows:
    tuple[tuple[InternalScalar, InternalScalar], ...]
RawTableOccurrence.defining_unit: UnitResolution
RawTableOccurrence.defined_unit: UnitResolution

RawReferenceOccurrence.reference_identity: ReferenceIdentityKind

RawComplexOccurrence.children: tuple[RawOccurrence, ...]

RawSimpleQuantityOccurrence.value: InternalScalar
RawSimpleQuantityOccurrence.quantity_dimension: UnitDimension

RawComplexQuantityOccurrence.children: tuple[RawOccurrence, ...]
```

Ciclos são detetados no traversal IFC antes de construir a árvore imutável.
`property_facts.py` nunca recebe uma estrutura cíclica.

### 4.5 Diagnostics e resultados

`property_facts.py` não importa `WarningCode` de `canonical_ifc.py`.

Define códigos internos fechados:

```python
class PropertyDiagnosticCode(str, Enum):
    UNSUPPORTED_PROPERTY_KIND = "unsupported_property_kind"
    REFERENCE_UNSUPPORTED_V1 = "reference_unsupported_v1"
    UNKNOWN_UNIT = "unknown_unit"
    INCOMPATIBLE_UNIT = "incompatible_unit"
    TYPE_OVERRIDE = "type_override"
    REDUNDANT_DUPLICATE = "redundant_duplicate"
    EMPTY_PROPERTY_NAME = "empty_property_name"
    NULL_ITEM = "null_item"
    EMPTY_LIST = "empty_list"
    EMPTY_ENUM = "empty_enum"
    EMPTY_TABLE = "empty_table"
    TABLE_LENGTH_MISMATCH = "table_length_mismatch"
    COMPLEX_CYCLE = "complex_cycle"
    NON_FINITE_VALUE = "non_finite_value"
    DEPTH_LIMIT_EXCEEDED = "depth_limit_exceeded"
    LIST_LIMIT_EXCEEDED = "list_limit_exceeded"
    TABLE_LIMIT_EXCEEDED = "table_limit_exceeded"
```

```python
@dataclass(frozen=True, slots=True)
class PropertyDiagnostic:
    code: PropertyDiagnosticCode
    origin: PropertyOrigin | None
    source: PropertySource
    ifc_kind: IfcPropertyKind
    reference: str | None
```

`reference` só pode conter identificador opaco permitido pelo contrato
HBIM-011, nunca nomes, valores, unidades ou paths.

```python
@dataclass(frozen=True, slots=True)
class PropertyCoverageDelta:
    scalar_facts: int = 0
    atomized_list_items: int = 0
    atomized_enum_items: int = 0
    atomized_bounded_values: int = 0
    atomized_table_cells: int = 0
    atomized_complex_leaves: int = 0
    unsupported_references: int = 0
    redundant_duplicates: int = 0
    type_overrides: int = 0
    non_integral_counts: int = 0
    null_collection_items: int = 0
    depth_limit_exceeded: int = 0
    list_limit_exceeded: int = 0
    table_limit_exceeded: int = 0
    non_finite_properties: int = 0
```

```python
@dataclass(frozen=True, slots=True)
class DedupDecision:
    fact_id: str
    kept_origin: PropertyOrigin
    dropped_origin: PropertyOrigin | None
    reason: DedupReason

@dataclass(frozen=True, slots=True)
class AtomizationResult:
    facts: tuple[PropertyFact, ...]
    diagnostics: tuple[PropertyDiagnostic, ...]
    coverage: PropertyCoverageDelta
    decisions: tuple[DedupDecision, ...]
```

---

## 5. Gramática de `occurrence_key`

### 5.1 Formas simples

```text
LEAF_OCCURRENCE :=
    "0"
  | "item:" INDEX
  | "lower"
  | "upper"
  | "setpoint"
  | "row:" INDEX ":defining"
  | "row:" INDEX ":defined"

INDEX := exatamente seis dígitos decimais
```

Intervalo válido:

```text
000000 .. 999999
```

### 5.2 Propriedades complexas

Uma folha dentro de uma propriedade complexa combina caminho e ocorrência:

```text
COMPLEX_OCCURRENCE :=
    "child:" SEGMENT_COUNT ":" ENCODED_PATH ":" LEAF_OCCURRENCE

SEGMENT_COUNT := exatamente seis dígitos decimais
ENCODED_PATH  := concatenação de SEGMENT_COUNT netstrings
netstring(s)  := len(utf8(s)) ":" utf8(s)
```

Exemplo:

```text
child:000002:7:Thermal6:Layers:item:000001
```

O parser:

1. lê `SEGMENT_COUNT`;
2. consome exatamente esse número de netstrings;
3. interpreta o restante como `LEAF_OCCURRENCE`.

O caminho contém os nomes completos desde o primeiro filho da complex property
até à folha.

### 5.3 Gramática completa

```text
occurrence_key :=
    LEAF_OCCURRENCE
  | COMPLEX_OCCURRENCE
```

Isto permite representar sem colisão:

- single dentro de complex;
- lista dentro de complex;
- enum dentro de complex;
- bounded dentro de complex;
- tabela dentro de complex;
- quantity dentro de complex quantity.

### 5.4 Regras obrigatórias

- nunca usar STEP entity id;
- nunca usar ordem de relações IFC;
- nunca usar timestamp ou path;
- nunca concatenar nomes livres sem length-prefix;
- mudar o valor não muda o `occurrence_key`;
- mudar a posição numa sequência muda o `occurrence_key`;
- itens duplicados têm índices distintos;
- caminhos com `:`, `/` ou Unicode são inequívocos.

---

## 6. Traversal raw de IFC

`ifc_properties.py` percorre:

### 6.1 Instância

```text
entity.IsDefinedBy
→ IfcRelDefinesByProperties
→ IfcPropertySet | IfcElementQuantity
```

### 6.2 Tipo

```text
IfcRelDefinesByType
→ RelatingType
→ HasPropertySets
→ IfcPropertySet | IfcElementQuantity
```

### 6.3 Regras

- coleta instância e tipo separadamente;
- não aplica precedência durante a leitura;
- produz `RawOccurrence` tipado;
- ordena relações/property sets por chave estrutural estável;
- não usa entity id para identidade;
- usa cache por type object e property set partilhado;
- usa cache de unidades do projeto;
- deteta ciclos antes de construir complex occurrences;
- nunca altera o modelo IFC;
- nunca cria rede ou lê `.env`.

---

## 7. Precedência instance versus type

A precedência ocorre **antes da atomização**.

Chave de propriedade:

```text
(source, container, property_name)
```

Regras:

- só instance → manter instance;
- só type → manter type;
- instance e type estruturalmente equivalentes → manter instance, deduplicar type;
- instance e type diferentes → manter a propriedade instance inteira;
- diferença produz `TYPE_OVERRIDE` + `type_overrides += 1`;
- listas não são combinadas posição a posição;
- tabelas não são combinadas linha a linha;
- bounded não herda limites residuais do tipo;
- complex property não herda folhas residuais do tipo;
- complex quantity não herda folhas residuais do tipo;
- origem não é persistida no `PropertyFact`.

Exemplo obrigatório:

```text
type:      Layers = ["A", "B"]
instance:  Layers = ["C"]
```

Saída:

```text
item:000000 = "C"
```

Nunca:

```text
item:000000 = "C"
item:000001 = "B"
```

---

## 8. Regras por tipo IFC

### 8.1 `IfcPropertySingleValue`

- um facto;
- `occurrence_key = "0"`;
- `null` explícito produz `NullScalar`;
- unidade explícita preservada;
- scalar parity byte-idêntica à HBIM-011.

### 8.2 `IfcPropertyEnumeratedValue`

- um facto por item;
- `item:NNNNNN`;
- ordem declarada;
- itens repetidos preservados;
- unidade da enum/propriedade quando resolvida;
- enum vazia → diagnostic `EMPTY_ENUM`, sem factos;
- null numa posição → `NullScalar` + `NULL_ITEM`.

### 8.3 `IfcPropertyListValue`

- um facto por item;
- `item:NNNNNN`;
- ordem declarada;
- itens repetidos preservados;
- lista vazia → diagnostic `EMPTY_LIST`, sem factos;
- null numa posição → `NullScalar` + `NULL_ITEM`.

### 8.4 `IfcPropertyBoundedValue`

- até três factos: `lower`, `upper`, `setpoint`;
- emitir apenas papéis presentes;
- `setpoint` apenas onde o schema IFC o suportar;
- a mesma unidade aplica-se aos três;
- null explícito num papel presente produz `NullScalar`;
- `NaN`/`Inf` em qualquer papel faz saltar a propriedade inteira.

### 8.5 `IfcPropertyTableValue`

Por linha:

```text
row:NNNNNN:defining
row:NNNNNN:defined
```

Regras:

- ordem das linhas preservada;
- `DefiningUnit` e `DefinedUnit` resolvidas separadamente;
- comprimentos diferentes → diagnostic `TABLE_LENGTH_MISMATCH`;
- tabela inteira omitida;
- nunca emitir linhas parciais;
- null numa célula produz `NullScalar` + `NULL_ITEM`;
- `NaN`/`Inf` em qualquer célula omite a tabela inteira;
- tabela vazia → `EMPTY_TABLE`.

Estrutura impossível que não permita pareamento determinístico lança
`TableStructureError`.

### 8.6 `IfcPropertyReferenceValue`

- zero factos;
- `reference_identity` apenas classifica a referência;
- diagnostic `REFERENCE_UNSUPPORTED_V1`;
- coverage `unsupported_references += 1`;
- nunca guardar o valor referido;
- nunca usar `str()`.

### 8.7 `IfcComplexProperty`

- emitir apenas folhas;
- cada folha usa `COMPLEX_OCCURRENCE`;
- o caminho inclui todos os nomes de filhos;
- listas/tabelas/bounded dentro da complex usam o sufixo `LEAF_OCCURRENCE`;
- ciclo → diagnostic `COMPLEX_CYCLE`;
- omitir a complex property inteira;
- nunca emitir factos parciais;
- exceder profundidade → omitir a propriedade inteira.

### 8.8 Quantidades simples

Suportar:

- `IfcQuantityLength`;
- `IfcQuantityArea`;
- `IfcQuantityVolume`;
- `IfcQuantityCount`;
- `IfcQuantityWeight`;
- `IfcQuantityTime`;
- outras subclasses de `IfcPhysicalSimpleQuantity` presentes em IFC2X3/IFC4,
  quando o valor escalar e a dimensão puderem ser determinados de forma fechada.

Regras:

- um facto;
- `occurrence_key = "0"`;
- unidade da quantity;
- parity com HBIM-011.

`IfcQuantityCount`:

- valor finito e matematicamente integral → `IntScalar`;
- valor finito não integral → `FloatScalar`;
- nunca truncar;
- incrementar `non_integral_counts`;
- sem warning fatal.

### 8.9 `IfcPhysicalComplexQuantity`

- folhas atomizadas com `COMPLEX_OCCURRENCE`;
- mesmas regras de ciclos, profundidade e atomicidade de complex properties;
- nunca emitir factos parciais da complex quantity.

---

## 9. Unidades

### 9.1 Precedência

1. unidade explícita da propriedade;
2. unidade explícita da quantity;
3. unidade efetiva da definição do tipo, quando aplicável;
4. unidade do projeto, apenas para dimensão conhecida;
5. ausência.

### 9.2 Mapa fechado de dimensões

Exemplos:

```text
IfcLengthMeasure       → LENGTH
IfcAreaMeasure         → AREA
IfcVolumeMeasure       → VOLUME
IfcMassMeasure         → MASS
IfcTimeMeasure         → TIME
IfcCountMeasure        → COUNT
IfcQuantityLength      → LENGTH
IfcQuantityArea        → AREA
IfcQuantityVolume      → VOLUME
IfcQuantityWeight      → MASS
IfcQuantityTime        → TIME
IfcQuantityCount       → COUNT
```

Expansões exigem testes IFC2X3 e IFC4.

### 9.3 Comportamento fechado

- unidade ausente → emitir facto com `unit=None`, sem warning;
- unidade presente e resolvida → emitir rótulo determinístico;
- unidade presente mas sem rótulo determinístico → emitir `unit=None` +
  diagnostic `UNKNOWN_UNIT`;
- unidade conhecida mas dimensionalmente incompatível → omitir a propriedade
  inteira + diagnostic `INCOMPATIBLE_UNIT`;
- nunca preservar `str(entity)`;
- nunca converter valores;
- nunca arredondar;
- não implementar equivalência `2000 mm = 2 m`.

A proveniência da unidade fica apenas em `UnitResolution` e coverage interno.

---

## 10. Valores null e não finitos

### 10.1 Null

- single null → emitir `PropertyFact` null;
- null em lista/enum/table → emitir `PropertyFact` null na posição original;
- emitir diagnostic `NULL_ITEM` apenas para coleções;
- null não remove os outros itens.

### 10.2 NaN e infinito

Se `NaN`, `+Inf` ou `-Inf` aparecer em qualquer parte atomizável:

- omitir a propriedade inteira;
- emitir diagnostic `NON_FINITE_VALUE`;
- incrementar `non_finite_properties`;
- nunca emitir apenas os restantes itens/limites/células;
- nunca serializar não finitos.

Para complex properties/quantities, um não finito numa folha omite a complex
property inteira, preservando atomicidade da propriedade composta.

---

## 11. Deduplicação e conflitos

### 11.1 Mesmo nível

Após atomização de propriedades do mesmo nível:

- mesmo slot, mesmo valor e unidade → emitir uma vez;
- incrementar `redundant_duplicates`;
- decisão `REDUNDANT_SAME_LEVEL`;
- slots disjuntos → união determinística;
- mesmo slot, valor ou unidade diferente →
  `AmbiguousPropertySlotError`.

Nunca escolher:

- menor valor lexicográfico;
- primeira relação IFC;
- menor STEP id;
- ordem de leitura.

### 11.2 Instância versus tipo

A precedência da secção 7 ocorre antes da atomização.

- equivalente → deduplicação;
- diferente → instance vence integralmente;
- `TYPE_OVERRIDE`;
- nenhum conflito fatal.

### 11.3 `fact_id` collision

Depois de construir os factos:

- factos logicamente equivalentes com o mesmo `fact_id` podem ser deduplicados;
- factos logicamente diferentes com o mesmo `fact_id` lançam
  `FactIdCollisionError`;
- nenhum output é publicado.

### 11.4 Containers repetidos

Dois `IfcPropertySet`/`IfcElementQuantity` com o mesmo nome e origem:

- propriedades disjuntas → união;
- mesmo slot e mesmo valor/unidade → dedup;
- mesmo slot com diferença → `AmbiguousPropertySlotError`;
- não usar STEP id para os distinguir.

---

## 12. Limites contra explosão

Constantes:

| Constante | Valor |
|---|---:|
| `MAX_COMPLEX_DEPTH` | 8 |
| `MAX_LIST_ITEMS` | 4096 |
| `MAX_TABLE_ROWS` | 4096 |
| `MAX_FACTS_PER_ELEMENT` | 10000 |

Justificação:

- profundidade 8 permite margem sobre estruturas usuais sem recursão patológica;
- 4096 cobre listas/tabelas reais extensas e limita explosões;
- 10000 factos por elemento é muito superior ao esperado em modelos normais.

### 12.1 Limites de propriedade

Ao exceder profundidade, itens ou linhas:

- omitir a propriedade inteira;
- diagnostic e coverage correspondentes;
- nenhum facto parcial;
- continuar com outras propriedades.

### 12.2 Limite por elemento

Ao exceder `MAX_FACTS_PER_ELEMENT`:

- lançar `FactsPerElementLimitError`;
- abortar toda a extração;
- descartar staging;
- não publicar `elements.jsonl`, warnings ou coverage parciais.

`FACTS_PER_ELEMENT_EXCEEDED` não é warning serializado, porque não existe output
publicado após o erro fatal.

---

## 13. Erros

Exceções novas, sob `CanonicalExtractionError`:

- `AmbiguousPropertySlotError`;
- `FactIdCollisionError`;
- `FactsPerElementLimitError`;
- `TableStructureError`.

Regras:

- preservam a causa;
- não contêm nomes de propriedades, valores, unidades ou paths;
- podem conter `ifc_class` e identificador opaco permitido;
- erro inesperado propaga;
- nunca existe `except Exception` amplo por propriedade.

Ciclos, mismatches de comprimento e limites de propriedade são condições
conhecidas não fatais e produzem diagnostics/coverage.

---

## 14. Warnings e coverage

### 14.1 Mapeamento

`canonical_ifc.py` faz um mapping total:

```text
PropertyDiagnosticCode
→ WarningCode + FieldCode + DetailCode
```

Não existe import inverso de `canonical_ifc.py` em `property_facts.py`.

### 14.2 Códigos serializados

Adicionar códigos fechados conforme necessário:

- `UNSUPPORTED_PROPERTY_KIND`;
- `REFERENCE_UNSUPPORTED_V1`;
- `UNKNOWN_UNIT`;
- `INCOMPATIBLE_UNIT`;
- `TYPE_OVERRIDE`;
- `REDUNDANT_DUPLICATE`;
- `EMPTY_PROPERTY_NAME`;
- `NULL_ITEM`;
- `EMPTY_LIST`;
- `EMPTY_ENUM`;
- `EMPTY_TABLE`;
- `TABLE_LENGTH_MISMATCH`;
- `COMPLEX_CYCLE`;
- `NON_FINITE_VALUE`;
- `DEPTH_LIMIT_EXCEEDED`;
- `LIST_LIMIT_EXCEEDED`;
- `TABLE_LIMIT_EXCEEDED`.

`VALUE_CONFLICT` e `UNIT_CONFLICT` podem ser detail codes de `TYPE_OVERRIDE`.
Conflitos no mesmo nível são fatais, não warnings.

### 14.3 Confidencialidade

Warnings e coverage nunca contêm:

- nomes de psets;
- nomes de propriedades;
- valores;
- unidades concretas;
- structural paths;
- ficheiros;
- pessoas;
- organizações;
- moradas.

Podem conter apenas:

- código fechado;
- `FieldCode`;
- `DetailCode`;
- `ifc_class`;
- GlobalId/hash opaco;
- contagens.

### 14.4 Coverage

Adicionar contadores inteiros, sempre presentes, com zero incluído:

- `atomized_list_items`;
- `atomized_enum_items`;
- `atomized_bounded_values`;
- `atomized_table_cells`;
- `atomized_complex_leaves`;
- `unsupported_references`;
- `redundant_duplicates`;
- `type_overrides`;
- `non_integral_counts`;
- `null_collection_items`;
- `depth_limit_exceeded`;
- `list_limit_exceeded`;
- `table_limit_exceeded`;
- `non_finite_properties`.

A versão do manifest é atualizada segundo a convenção já existente no
`CoverageReport`; a alteração é documentada nos golden files.

---

## 15. Determinismo

Testar:

- execução repetida;
- relações IFC em ordens diferentes;
- property sets em ordens diferentes;
- instance/type;
- listas e enums;
- bounded;
- tabelas;
- complex paths;
- complex + list/table/bounded;
- duplicate containers;
- duplicate list items;
- mudança de valor sem mudança de `fact_id`;
- Unicode e caracteres `:`/`/`;
- null;
- não finitos;
- conflitos no mesmo nível.

Nunca usar nos IDs:

- STEP id;
- ordem de relação;
- timestamp;
- path absoluto;
- checksum;
- valor;
- unidade.

Ordenação final:

```text
(container, property_name, occurrence_key, source)
```

---

## 16. Compatibilidade e migração incremental

### 16.1 Inalterado

- APIs públicas de conversão;
- atomic writer;
- `ElementRecord`;
- spatial;
- materiais;
- classificações;
- documentos;
- métricas HBIM-011;
- extractor legacy;
- indexer;
- API;
- retrieval;
- mappings;
- frontend;
- `backend/canonical`;
- `backend/eval`;
- baseline HBIM-005.

### 16.2 Alterado

- produtor interno de `PropertyFact`;
- warnings/coverage de propriedades;
- golden files dependentes de propriedades.

### 16.3 Métricas

As métricas podem continuar com `get_psets`. Este caminho é independente e não
pode produzir ou alterar `PropertyFact`.

---

## 17. Golden files

### 17.1 Devem permanecer byte-idênticos

- `elements.jsonl`;
- `classification_facts.jsonl`;
- `documents.jsonl`;
- golden HBIM-010;
- factos single e quantities simples já existentes;
- qualquer fixture não dependente de propriedades/warnings/coverage.

### 17.2 Podem mudar intencionalmente

- `property_facts.jsonl`;
- `warnings.jsonl`;
- `coverage.json`.

Mudanças esperadas:

- listas antes classificadas como planned atomization passam a factos;
- novos tipos complexos produzem factos;
- warnings de complex values diminuem;
- surgem novos diagnostics;
- coverage ganha novos contadores.

Qualquer alteração em `elements`, classifications ou documents é regressão até
prova em contrário.

### 17.3 Baseline HBIM-005

Registar o SHA-256 antes e depois. Deve permanecer byte-idêntica.

---

## 18. Fixtures sintéticas

Apenas IFCs sintéticos em `tmp_path`.

Builders/cenários separados:

- single;
- enum;
- list;
- bounded IFC4;
- bounded IFC2X3;
- table;
- table mismatch;
- reference;
- complex single;
- complex list;
- complex bounded;
- complex table;
- complex cycle ou teste puro equivalente;
- simple quantities;
- complex quantity;
- instance-only;
- type-only;
- equal instance/type;
- override instance/type;
- duplicate relation;
- duplicate container;
- same-level conflict;
- duplicate list item;
- explicit units;
- project units;
- unknown unit;
- incompatible unit;
- empty/null;
- NaN/Inf;
- depth/list/table limits;
- facts-per-element limit.

Nenhum `.ifc` é committed. Nenhum builder usa `local_data`.

---

## 19. Testes

### 19.1 Suite pura de `property_facts.py`

Sem IfcOpenShell:

- grammar e parser;
- netstrings;
- complex + cada leaf occurrence;
- precedência property-level;
- dedup;
- same-level conflicts;
- fact-id collision;
- limits;
- null;
- non-finite;
- unit rules;
- Count integral/não integral.

### 19.2 Suite IFC de `ifc_properties.py`

- IFC2X3;
- IFC4;
- traversal instance;
- traversal type;
- units;
- raw variants;
- reference;
- complex;
- quantity;
- cycles;
- caches;
- ordem independente.

### 19.3 Integração em `canonical_ifc.py`

- scalar parity;
- golden output;
- diagnostics mapping;
- coverage;
- atomic writer;
- no partial output;
- public APIs unchanged;
- repeated execution;
- random test order.

### 19.4 Segurança

- import-safety por subprocess;
- `property_facts.py` sem IfcOpenShell;
- sem OpenSearch/FastAPI/settings;
- sem leitura de `.env`;
- sem sockets;
- nenhum IFC real tracked.

---

## 20. Ficheiros previstos

### Criar

- `backend/ingestion/property_facts.py`;
- `backend/ingestion/ifc_properties.py`;
- `backend/tests/test_property_facts.py`;
- `backend/tests/test_ifc_properties.py`;
- `docs/implementation/issues/HBIM-012_PROPERTY_FACT_ATOMIZATION.md`.

### Modificar na implementação

- `backend/ingestion/canonical_ifc.py`;
- `backend/tests/fixtures/ifc_builder.py` ou builder dedicado;
- `backend/tests/test_canonical_ifc.py`;
- `backend/tests/test_canonical_ifc_import_safety.py`;
- `backend/tests/fixtures/canonical/ifc_extraction/property_facts.jsonl`;
- `backend/tests/fixtures/canonical/ifc_extraction/warnings.jsonl`;
- `backend/tests/fixtures/canonical/ifc_extraction/coverage.json`;
- `pyproject.toml`;
- `.github/workflows/ci.yml`;
- `docs/development/LOCAL_SETUP.md`;
- `docs/implementation/IMPLEMENTATION_STATUS.md`.

### Não tocar

- `backend/canonical/**`;
- `extract_bim.py`;
- `index_to_opensearch.py`;
- `backend/api/**`;
- `backend/eval/**`;
- `frontend/**`;
- mappings;
- `.gitignore`;
- `local_data/**`.

---

## 21. Tooling e CI

- `property_facts.py` e `ifc_properties.py` entram no gate mypy bloqueante;
- Ruff limpo;
- testes offline no job `backend-unit`;
- sem novo job CI;
- sem serviços externos;
- sem ML;
- `evaluation-opensearch` inalterado;
- suite completa validada com:
  - seed `77082843`;
  - seed `1`;
  - `-p no:randomly`.

---

## 22. Critérios de aceitação

1. **Scalar parity** — singles e quantities simples mantêm `occurrence_key="0"`,
   `fact_id` e bytes.
2. **Traversal raw** — `PropertyFact` deixa de usar `get_psets`.
3. **Dependências** — `property_facts.py` é puro; direção
   `ifc_properties → property_facts`.
4. **Modelos tipados** — sem `Any`, dicts livres ou entidades IFC no módulo puro.
5. **Grammar simples** — `0`, item, bounded e table determinísticos.
6. **Grammar complexa** — segment count + netstrings + leaf occurrence.
7. **Complex multivalue** — list/enum/bounded/table dentro de complex sem colisões.
8. **Lists/enums** — um facto por índice; ordem e duplicados preservados.
9. **Bounded** — lower/upper/setpoint; sem partial output.
10. **Tables** — defining/defined; mismatch omite a tabela inteira.
11. **References** — zero factos; sem `str()`; coverage explícito.
12. **Complex** — folhas; ciclos/limites omitem a propriedade inteira.
13. **Quantities** — tipos simples e complex quantity.
14. **Count** — integral→int; não integral→float; nunca truncado.
15. **Property-level precedence** — instance substitui a propriedade type inteira.
16. **No mixed sequences** — listas/tabelas/complex nunca combinam instância e tipo.
17. **Dedup** — redundantes idempotentes; disjuntos unidos.
18. **Same-level conflict** — `AmbiguousPropertySlotError`.
19. **Fact ID collision** — `FactIdCollisionError`.
20. **Units** — regras fechadas para absent/unknown/incompatible.
21. **Null** — preservado; diagnostic apenas em coleções.
22. **Non-finite** — propriedade inteira omitida; sem partial output.
23. **Limits** — propriedade excedida é omitida integralmente.
24. **Facts per element** — erro fatal e nenhum output publicado.
25. **Warnings/coverage** — códigos fechados e sem informação privada.
26. **Determinismo** — relações reordenadas não alteram output.
27. **Golden impact** — apenas property facts/warnings/coverage mudam intencionalmente.
28. **API/atomic writer** — assinaturas e publicação atómica inalteradas.
29. **Import-safety** — sem env, sockets ou dependências externas no import.
30. **Legacy/baseline** — intactos e SHA-256 da baseline idêntico.
31. **Ruff/mypy/CI** — gates verdes, sem novo job.
32. **Nenhum IFC real** — nenhum `.ifc` committed; `local_data` ignorado.

---

## 23. Riscos residuais

- explosão de factos mitigada por limites explícitos;
- enum como sequência faz reordenação alterar IDs;
- referências continuam indisponíveis como factos em v1.0;
- `get_psets` continua temporariamente nas métricas;
- fail-closed pode rejeitar IFCs patológicos com slots ambíguos;
- origem e unidade normalizada continuam indisponíveis no schema v1.0.

---

## 24. Fora de scope

- conversão numérica de unidades;
- `unit_norm` e `value_norm`;
- origem persistida;
- reference-as-value;
- ontologias e aliases;
- tradução multilingue;
- equivalência semântica entre vendors;
- alteração de `backend/canonical`;
- schema 1.1;
- indexação OpenSearch;
- Neo4j;
- routing/retrieval;
- API/frontend;
- extractor legacy;
- baseline HBIM-005.

---

## 25. Questões bloqueantes

Nenhuma.

As decisões arquiteturais estão fechadas:

- schema v1.0 permanece;
- scalar parity usa `"0"`;
- property-level override;
- complex occurrence composta;
- módulos puros sem dependência circular;
- referência apenas em coverage;
- unidade sem conversão;
- conflicts no mesmo nível são fail-closed;
- limites nunca truncam;
- facts-per-element é erro fatal.

**SPEC READY FOR IMPLEMENTATION**