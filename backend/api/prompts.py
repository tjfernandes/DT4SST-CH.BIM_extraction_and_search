import textwrap

# ── Mapa de referência IFC ────────────────────────────────────────────
# Uma linha por termo, no formato "termo -> ifc_class", para que modelos
# pequenos consigam mapear sem interpretar tabelas markdown.
IFC_CLASS_TABLE = """\
porta -> IfcDoor
portas -> IfcDoor
door -> IfcDoor
doors -> IfcDoor
janela -> IfcWindow
janelas -> IfcWindow
window -> IfcWindow
windows -> IfcWindow
parede -> IfcWall
paredes -> IfcWall
wall -> IfcWall
walls -> IfcWall
muro -> IfcWall
laje -> IfcSlab
lajes -> IfcSlab
pavimento -> IfcSlab
slab -> IfcSlab
floor slab -> IfcSlab
pilar -> IfcColumn
pilares -> IfcColumn
coluna -> IfcColumn
colunas -> IfcColumn
column -> IfcColumn
columns -> IfcColumn
viga -> IfcBeam
vigas -> IfcBeam
beam -> IfcBeam
beams -> IfcBeam
escada -> IfcStair
escadas -> IfcStair
stair -> IfcStair
stairs -> IfcStair
staircase -> IfcStair
telhado -> IfcRoof
cobertura -> IfcRoof
roof -> IfcRoof
rampa -> IfcRamp
rampas -> IfcRamp
ramp -> IfcRamp
ramps -> IfcRamp
fachada cortina -> IfcCurtainWall
curtain wall -> IfcCurtainWall
guarda -> IfcRailing
guardas -> IfcRailing
corrimão -> IfcRailing
corrimao -> IfcRailing
railing -> IfcRailing
handrail -> IfcRailing
mobiliário -> IfcFurnishingElement
mobiliario -> IfcFurnishingElement
móvel -> IfcFurnishingElement
movel -> IfcFurnishingElement
móveis -> IfcFurnishingElement
moveis -> IfcFurnishingElement
furniture -> IfcFurnishingElement
furnishing -> IfcFurnishingElement
placa -> IfcPlate
placas -> IfcPlate
plate -> IfcPlate
plates -> IfcPlate
membro -> IfcMember
member -> IfcMember
members -> IfcMember
abertura -> IfcOpeningElement
aberturas -> IfcOpeningElement
opening -> IfcOpeningElement
openings -> IfcOpeningElement
revestimento -> IfcCovering
revestimentos -> IfcCovering
covering -> IfcCovering
coverings -> IfcCovering
genérico -> IfcBuildingElementProxy
generico -> IfcBuildingElementProxy
proxy -> IfcBuildingElementProxy
artefacto -> IfcBuildingElementProxy
artefactos -> IfcBuildingElementProxy
artefato -> IfcBuildingElementProxy
artefatos -> IfcBuildingElementProxy
artifact -> IfcBuildingElementProxy
artifacts -> IfcBuildingElementProxy
tubo -> IfcFlowSegment
tubagem -> IfcFlowSegment
pipe -> IfcFlowSegment
pipes -> IfcFlowSegment
pipe segment -> IfcFlowSegment
válvula -> IfcFlowController
valvula -> IfcFlowController
controlador -> IfcFlowController
valve -> IfcFlowController
valves -> IfcFlowController
flow controller -> IfcFlowController
torneira -> IfcFlowTerminal
sanita -> IfcFlowTerminal
terminal -> IfcFlowTerminal
flow terminal -> IfcFlowTerminal
acessório -> IfcFlowFitting
acessorio -> IfcFlowFitting
fitting -> IfcFlowFitting
fittings -> IfcFlowFitting
flow fitting -> IfcFlowFitting
"""

REWRITE_QUERY = textwrap.dedent("""\
A pergunta do utilizador pode conter referências vagas a mensagens anteriores (ex: "elas", "esses", "mostra-me", "listá-las").
Se a pergunta JÁ é clara e auto-contida, devolve-a EXATAMENTE como está, sem alterar.
Se contém referências vagas, substitui APENAS os pronomes/referências pelo termo correto do histórico.

[Regras]
- NÃO adiciones informação que não esteja no histórico nem na pergunta original.
- NÃO mudes a intenção da pergunta.
- NÃO traduzas nem reformules a frase — mantém a língua e estilo do utilizador.
- NÃO adiciones termos técnicos, explicações ou contexto extra.
- Se a pergunta já faz sentido sozinha, devolve-a EXATAMENTE como está.
- Devolve APENAS a pergunta reescrita, sem explicações.

[Exemplos]
Histórico: "quantas paredes existem?" → "Existem 7 paredes."
Pergunta: "consegues listá-las?"
Reescrita: "consegues listar as paredes?"

Histórico: "mostra-me as portas do piso 1" → "Encontrei 3 portas..."
Pergunta: "e do piso 2?"
Reescrita: "mostra-me as portas do piso 2"

Pergunta: "quantos elementos encontras do projeto Mosteiro de Santa Clara a Velha?"
Reescrita: "quantos elementos encontras do projeto Mosteiro de Santa Clara a Velha?"

Pergunta: "mostra-me as paredes do piso 1"
Reescrita: "mostra-me as paredes do piso 1"

Histórico de mensagens:
{history}

Pergunta do utilizador:
"{user_input}"
""")


# ── Passo 1: Classificar intenção + estratégia de pesquisa ────────────
CLASSIFY_INTENT = textwrap.dedent("""\
Analisa a pergunta do utilizador e determina a estratégia de pesquisa.

[Estratégias de Pesquisa]
- "chat"        → Conversa geral, não precisa de dados do modelo BIM.
- "structured"  → Todos os critérios podem ser mapeados a filtros exatos (classe IFC, piso, nome, condições numéricas). Não há termos vagos ou conceptuais.
- "semantic"    → A pergunta contém termos como (piso, material, cor, tipo, nomes, propriedades) vagos, conceptuais, descritivos ou funcionais que não se traduzem em filtros exatos. Necessita pesquisa por similaridade semântica. Pode também ter filtros estruturados (piso, material, classe IFC) que serão usados como pré-filtros.
- "aggregation" → O utilizador quer um RESUMO, CONTAGEM ou LISTA de valores DISTINTOS de um campo (ex: "quais materiais existem", "quantas paredes há", "lista os pisos", "que tipos existem"). O objetivo é obter estatísticas ou valores únicos, NÃO ver elementos individuais.
- "detail"      → O utilizador faz uma pergunta sobre um elemento JÁ apresentado nos resultados anteriores (ex: "fala-me mais sobre esse", "que propriedades tem o primeiro?", "e essa viga?", "detalha o terceiro"). Requer acesso ao documento completo.

[Campos de saída]
1. search_strategy – "chat", "structured", "semantic", "aggregation" ou "detail"

[Regras]
- Usa "chat" se a pergunta NÃO é sobre elementos do modelo (ex: "olá", "o que é o IFC?", "como estás?").
- Usa "detail" se o utilizador refere um elemento ESPECÍFICO dos resultados anteriores (ex: "fala-me mais sobre esse", "que propriedades tem o primeiro?", "detalha o segundo", "e essa viga?", "diz-me mais sobre este elemento"). Requer que haja resultados no histórico.
- Usa "aggregation" APENAS se o utilizador pede explicitamente contagens, valores distintos ou estatísticas (ex: "quantas", "quais materiais existem", "que tipos há"). Palavras como "listar", "mostrar", "ver" referem-se a elementos individuais → usa "structured" ou "semantic".
- Se a pergunta é um follow-up vago (ex: "consegues listá-las?", "mostra-me", "sim"), interpreta no contexto do histórico. Se o contexto anterior era sobre elementos, usa "structured".
- Usa "structured" se TODOS os critérios podem ser expressos como filtros exatos (classe, dimensões numéricas), ou se o utilizador quer VER elementos individuais.
- Usa "semantic" se a pergunta contém termos vagos, descritivos, conceptuais, funcionais ou relações que não se mapeiam a filtros exatos.
- Devolve apenas um objeto JSON válido.
                                  
[Exemplos]
Pergunta: "olá, como estás?"
→ {{"search_strategy": "chat"}}

Pergunta: "mostra-me as portas do piso 1"
→ {{"search_strategy": "structured"}}

Pergunta: "paredes de betão com mais de 3 metros"
→ {{"search_strategy": "structured"}}

Pergunta: "mostra-me todos os elementos do piso 2"
→ {{"search_strategy": "structured"}}

Pergunta: "artefactos de calcário"
→ {{"search_strategy": "structured"}}

Pergunta: "vigas com mais de 5 metros"
→ {{"search_strategy": "structured"}}

Pergunta: "lista todos os materiais das paredes"
→ {{"search_strategy": "aggregation"}}

Pergunta: "quais são os pisos do edifício?"
→ {{"search_strategy": "aggregation"}}

Pergunta: "quantas portas existem por piso?"
→ {{"search_strategy": "aggregation"}}

Pergunta: "que tipos de elementos existem no modelo?"
→ {{"search_strategy": "aggregation"}}

Pergunta: "quantos elementos tem o modelo?"
→ {{"search_strategy": "aggregation"}}

Pergunta: "lista as paredes"
→ {{"search_strategy": "structured"}}
                                  
Pergunta: "Que elementos contêm documentos associados?"
→ {{"search_strategy": "semantic"}}
                                  
Pergunta: "Que elementos contêm classificações associadas?"
→ {{"search_strategy": "semantic"}}                                  

Pergunta: "elementos estruturais do edifício"
→ {{"search_strategy": "semantic"}}

Pergunta: "tudo relacionado com a fachada"
→ {{"search_strategy": "semantic"}}

Pergunta: "paredes com anomalias ou patologias"
→ {{"search_strategy": "semantic"}}

Pergunta: "componentes de proteção contra incêndio"
→ {{"search_strategy": "semantic"}}

Pergunta: "o que suporta o telhado?"
→ {{"search_strategy": "semantic"}}

Pergunta: "elementos decorativos da fachada principal"
→ {{"search_strategy": "semantic"}}

Pergunta: "elementos mais antigos do modelo"
→ {{"search_strategy": "semantic"}}

Pergunta: "fala-me mais sobre esse elemento"
→ {{"search_strategy": "detail"}}

Pergunta: "que propriedades tem o primeiro?"
→ {{"search_strategy": "detail"}}

Pergunta: "detalha o terceiro resultado"
→ {{"search_strategy": "detail"}}

Pergunta: "e essa viga?"
→ {{"search_strategy": "detail"}}

Pergunta do utilizador:
"{user_input}"
""")

# ── Passo 1b: Extrair classe IFC ──────────────────────────────────────
EXTRACT_IFC_CLASS = textwrap.dedent("""\
Identifica a classe IFC mencionada na pergunta do utilizador.

[Mapa termo -> ifc_class]
{ifc_table}

[Campos de saída]
1. ifc_class – a classe IFC do objeto mencionado (da tabela acima), ou null

[Regras]
- Cada linha do mapa tem este formato: termo -> ifc_class.
- Procura na pergunta um termo que esteja à esquerda de "->".
- Se encontrares correspondência, devolve exatamente a classe IFC à direita de "->" (PascalCase, case-sensitive).
- Não devolvas o termo encontrado. Devolve só a classe IFC.
- Se NÃO encontrares correspondência ou não tiveres certeza, usa ifc_class=null.
- Se a pergunta mencionar "elementos" de forma genérica, sem especificar tipo, usa ifc_class=null.
- Retorna APENAS UM valor. Se houver múltiplos tipos, escolhe o principal.
- Devolve apenas um objeto JSON válido.

[Exemplos]
Pergunta: "mostra-me as portas do piso 1"
→ {{"ifc_class": "IfcDoor"}}

Pergunta: "paredes de betão com mais de 3 metros"
→ {{"ifc_class": "IfcWall"}}

Pergunta: "mostra-me todos os elementos do piso 2"
→ {{"ifc_class": null}}

Pergunta: "artefactos de calcário"
→ {{"ifc_class": "IfcBuildingElementProxy"}}

Pergunta: "vigas com mais de 5 metros"
→ {{"ifc_class": "IfcBeam"}}

Pergunta: "elementos estruturais do edifício"
→ {{"ifc_class": null}}

Pergunta: "tudo relacionado com a fachada"
→ {{"ifc_class": null}}

Pergunta: "as escadas do rés-do-chão"
→ {{"ifc_class": "IfcStair"}}

Pergunta do utilizador:
"{user_input}"
""")

# ── Passo 2: Extrair filtros textuais ─────────────────────────────────
EXTRACT_FILTERS = textwrap.dedent("""\
Dada a pergunta do utilizador sobre elementos BIM, extrai os filtros de TEXTO.

Retorna um JSON com estes campos (usa null se não mencionado):
- name      → nome/designação do elemento (string ou null)
- project_id → identificador do projeto (string ou null)
- project_name → nome do projeto (string ou null)

[Regras]
1. name: extrai o nome se mencionado (ex: "Artifact_0", "porta principal"). Usa null se não mencionado.
2. project_id: NUNCA inferir. Só extrai se a pergunta disser explicitamente que o valor é um ID de projeto, usando expressões como "project_id", "project id", "id do projeto", "id de projeto", "id do projecto" ou "identificador do projeto".
3. Se a pergunta disser apenas "projeto X", "modelo X" ou mencionar um nome de projeto, isso NÃO é project_id. Nesse caso usa project_name.
4. project_name: extrai se o utilizador mencionar o nome de um projeto específico. Usa null se não mencionado.
5. Se houver dúvida entre project_id e project_name, usa project_id=null e coloca o valor em project_name.

[Exemplos]
Pergunta: "portas de madeira do piso 1"
→ {{"name": null, "project_id": null}}

Pergunta: "mostra-me o Artifact_0"
→ {{"name": "Artifact_0", "project_id": null}}

Pergunta: "elementos de calcário do nível L0"
→ {{"name": null, "project_id": null}}

Pergunta: "paredes com mais de 3 metros"
→ {{"name": null, "project_id": null}}

Pergunta: "artefactos de granito"
→ {{"name": null, "project_id": null}}
                                  
Pergunta: "elementos do projeto Mosteiro de Santa Clara a Velha"
→ {{"name": null, "project_id": null, "project_name": "Mosteiro de Santa Clara a Velha"}}

Pergunta: "elementos do projeto SCV_2024"
→ {{"name": null, "project_id": null, "project_name": "SCV_2024"}}

Pergunta: "elementos com project_id SCV_2024"
→ {{"name": null, "project_id": "SCV_2024", "project_name": null}}

Pergunta: "elementos com id do projeto SCV_2024"
→ {{"name": null, "project_id": "SCV_2024", "project_name": null}}

Pergunta do utilizador:
"{user_input}"
""")

# ── Passo 3: Extrair condições numéricas ──────────────────────────────
EXTRACT_CONDITIONS = textwrap.dedent("""\
Dada a pergunta do utilizador sobre elementos BIM, extrai APENAS condições NUMÉRICAS.

[Fields disponíveis]
- height    → altura (metros)
- area      → área (m²)
- volume    → volume (m³)
- thickness → espessura / largura (metros)

[Operadores]
- eq       → valor exato ("exatamente X")
- approx   → valor aproximado ("X metros", sem 'exatamente') — margem ±0.5
- gt       → maior que ("mais de", "maior que", "acima de")
- gte      → maior ou igual ("pelo menos", "no mínimo")
- lt       → menor que ("menos de", "menor que", "abaixo de")
- lte      → menor ou igual ("no máximo")

[Regras]
1. Apenas campos numéricos: height, area, volume, thickness.
2. "exatamente X" → op="eq"
3. Apenas o número ("2 metros") → op="approx"
4. "mais de X" → op="gt", "pelo menos X" → op="gte"
5. "menos de X" → op="lt", "no máximo X" → op="lte"
6. Se não houver condições numéricas, retorna lista vazia [].
7. Devolve apenas um objeto JSON válido.

[Exemplos]
Pergunta: "portas de madeira com mais de 2 metros"
→ {{"conditions": [{{"field": "height", "op": "gt", "value": 2.0}}]}}

Pergunta: "janelas com exatamente 1.5 metros de altura"
→ {{"conditions": [{{"field": "height", "op": "eq", "value": 1.5}}]}}

Pergunta: "paredes com área superior a 10 m²"
→ {{"conditions": [{{"field": "area", "op": "gt", "value": 10.0}}]}}

Pergunta: "elementos do piso 1"
→ {{"conditions": []}}

Pergunta: "mostra-me todos os artefactos de calcário"
→ {{"conditions": []}}

Pergunta: "vigas de betão com mais de 5 metros e espessura inferior a 0.3"
→ {{"conditions": [
    {{"field": "height", "op": "gt", "value": 5.0}},
    {{"field": "thickness", "op": "lt", "value": 0.3}}
  ]}}

Pergunta do utilizador:
"{user_input}"
""")

EXTRACT_EMBEDDING_QUERY = textwrap.dedent("""\
Cria uma query textual para pesquisa vetorial em OpenSearch.

Esta query vai ser transformada em embedding e comparada com o campo semantic_text dos documentos BIM.
O semantic_text dos documentos usa linhas parecidas com:
- project: nome do projeto
- name: nome do elemento
- ifc_class: classe IFC e termos relacionados
- materials: materiais
- storey: piso/nível
- classifications: classificações associadas
- documents: documentos associados
- properties: propriedades descritivas

Pergunta original:
"{user_input}"

Contexto estruturado extraído:
- ifc_class: {ifc_class}
- filters: {filters_json}
- conditions: {conditions_json}

[Campo de saída]
1. embedding_query – texto curto e útil para embeddings

[Regras]
- Não cries uma query DSL de OpenSearch. Isto é apenas texto para embedding.
- Mantém os termos importantes da pergunta original.
- Usa palavras que provavelmente aparecem em semantic_text: ifc_class, name, materials, storey, classifications, documents, properties.
- Inclui a classe IFC, material, piso, nome ou propriedade apenas se estiverem na pergunta ou no contexto estruturado.
- Não inventes IDs, nomes de projeto, materiais, pisos, classificações, documentos nem propriedades.
- Se a pergunta procura documentos, inclui "documents".
- Se a pergunta procura classificações, inclui "classifications".
- Se a pergunta procura propriedades/características, inclui "properties".
- Pode ser uma frase curta ou várias linhas no estilo "campo: valor".
- Devolve apenas um objeto JSON válido.

[Exemplos]
Pergunta: "Que elementos contêm documentos associados?"
→ {{"embedding_query": "documents associated documents"}}

Pergunta: "paredes com anomalias ou patologias"
→ {{"embedding_query": "ifc_class: IfcWall wall parede\nproperties: anomalies pathologies anomalias patologias"}}

Pergunta: "elementos estruturais do edifício"
→ {{"embedding_query": "structural building elements properties classifications"}}

Pergunta: "tudo relacionado com a fachada"
→ {{"embedding_query": "facade fachada exterior wall curtain wall covering"}}
""")

FINAL_RESPONSE_FORMAT = textwrap.dedent("""
- Pergunta do utilizador: "{user_input}"
- A mostrar resultados {showing} de {total} no total
- Resultados da pesquisa:
{results}

Gera uma resposta clara e concisa para o utilizador, na lingua em que foi feita a pergunta, explicando os resultados encontrados.
Indica que estás a mostrar os resultados {showing} de {total} no total, apenas se não forem iguais.
Não refiras ids dos resultados.
Se o nome de um elemento for apenas um código (ex: "WD"), usa o ifc_class para dar contexto (ex: "a parede WD"). Se o nome for mais descritivo (ex: "porta principal"), usa só o nome.
Se não houver resultados, indica que nada foi encontrado.
IMPORTANTE: Sempre que apresentares um URL ou caminho de ficheiro (ex: https://... ou docs/...), formata-o OBRIGATORIAMENTE como hiperligação Markdown: [texto descritivo](url_ou_caminho). NUNCA uses backticks nem texto simples para URLs ou caminhos.
""")

# ── Filtro LLM de relevância (batch) ──────────────────────────────────
FILTER_RESULTS_BATCH = textwrap.dedent("""\
O utilizador perguntou: "{user_input}"

Estes resultados foram devolvidos pela pesquisa:
{results}

Para CADA resultado (identificado pelo índice [1], [2], etc.), decide se é relevante para a pergunta do utilizador.
Retorna JSON: {{"relevant_indices": [1, 3, 5]}} — lista dos índices dos resultados relevantes.
Se nenhum for relevante, retorna {{"relevant_indices": []}}.

[Regras]
- Relevante = o tipo (ifc_class), nome, material, localização, métricas ou propriedades correspondem ao que o utilizador pediu.
- Em caso de dúvida, considera relevante.
""")

# ── Passo para agregações: extrair o campo a agregar ──────────────
EXTRACT_AGGREGATION = textwrap.dedent("""\
O utilizador quer um resumo, lista ou contagem de valores sobre elementos BIM.

Pergunta: "{user_input}"

[Campos agregáveis]
- count           → o utilizador quer saber QUANTOS elementos existem (contagem total)
- material        → materiais dos elementos
- ifc_class       → tipos/classes IFC dos elementos
- storey          → pisos/andares/níveis
- classification  → classificações (ex: Uniclass, OmniClass)
- project         → projetos/modelos distintos, quando o utilizador pede projetos sem falar em project_id
- project_id      → IDs de projeto distintos, APENAS quando o utilizador diz explicitamente "project_id", "project id", "id do projeto" ou equivalente

[Campo de saída]
1. agg_field – o campo a agregar (um dos acima)

[Regras]
- Se o utilizador pede "quantos X existem?" ou "número de X" sobre um TIPO DE ELEMENTO (paredes, portas, etc.), usa agg_field="count".
- Se o utilizador pede "quantos projetos", "quantos modelos" ou quer saber QUE projetos existem, usa agg_field="project".
- NUNCA uses agg_field="project_id" só porque a pergunta menciona "projeto" ou "modelo".
- Usa agg_field="project_id" apenas se a pergunta disser explicitamente que quer project_id/id do projeto.
- Se o utilizador pede "quais materiais" ou "lista de materiais", usa agg_field="material".
- Se o utilizador pede "tipos de elementos", usa agg_field="ifc_class".
- Se o utilizador pede "quantos por piso" ou "elementos por andar", usa agg_field="storey".
- Devolve apenas um objeto JSON válido.

[Exemplos]
Pergunta: "quantas paredes existem?"
→ {{"agg_field": "count"}}

Pergunta: "número de IfcBuildingElementProxy"
→ {{"agg_field": "count"}}

Pergunta: "lista todos os materiais das paredes"
→ {{"agg_field": "material"}}

Pergunta: "quais são os pisos do edifício?"
→ {{"agg_field": "storey"}}

Pergunta: "que tipos de elementos existem?"
→ {{"agg_field": "ifc_class"}}

Pergunta: "quantas portas existem por piso?"
→ {{"agg_field": "storey"}}

Pergunta: "materiais dos pilares"
→ {{"agg_field": "material"}}

Pergunta: "classificações das vigas"
→ {{"agg_field": "classification"}}

Pergunta: "quantos projetos HBIM tenho?"
→ {{"agg_field": "project"}}

Pergunta: "quais são os meus projetos?"
→ {{"agg_field": "project"}}

Pergunta: "quantos modelos existem?"
→ {{"agg_field": "project"}}

Pergunta: "quantos project_id distintos existem?"
→ {{"agg_field": "project_id"}}
""")

# ── Passo para detail: extrair referência ao resultado ────────────────
EXTRACT_DETAIL_REF = textwrap.dedent("""\
O utilizador fez uma pergunta sobre um elemento específico dos resultados anteriores.
Os resultados anteriores tinham {num_results} elementos (índices 1 a {num_results}).

Pergunta: "{user_input}"

[Campo de saída]
1. index – o índice (1-based) do resultado a que o utilizador se refere

[Regras]
- Se o utilizador diz "o primeiro", "o 1º", "esse" (singular, sem número) → index=1
- Se o utilizador diz "o segundo", "o 2º" → index=2
- Se o utilizador diz "o terceiro" → index=3
- Se o utilizador diz "o último" → index={num_results}
- Se o utilizador refere pelo nome (ex: "a viga BMR-007"), tenta mapear ao resultado correto.
- Se não for claro, assume index=1.
- Devolve apenas um objeto JSON válido: {{"index": N}}

[Exemplos]
Pergunta: "fala-me mais sobre esse"
→ {{"index": 1}}

Pergunta: "que propriedades tem o segundo?"
→ {{"index": 2}}

Pergunta: "detalha o último"
→ {{"index": {num_results}}}
""")

DETAIL_RESPONSE_FORMAT = textwrap.dedent("""\
Pergunta do utilizador: "{user_input}"

Documento completo do elemento:
{document}

Gera uma resposta clara e detalhada para o utilizador, na lingua em que foi feita a pergunta.
Apresenta TODAS as informações pedidas do elemento: nome, classe IFC, piso, materiais, métricas, classificações, documentos e propriedades.
Se o utilizador perguntou algo específico (ex: propriedades, materiais), foca a resposta nisso mas inclui contexto.
IMPORTANTE: Sempre que apresentares um URL ou caminho de ficheiro, formata-o OBRIGATORIAMENTE como hiperligação Markdown: [texto descritivo](url_ou_caminho).
""")

AGGREGATION_RESPONSE_FORMAT = textwrap.dedent("""\
Pergunta do utilizador: "{user_input}"

Resultados da agregação ({agg_field}):
{results}

Gera uma resposta clara e concisa para o utilizador, na lingua em que foi feita a pergunta, apresentando os resultados da agregação.
Se houver contagens, apresenta-as. Formata como lista ou tabela Markdown se adequado.
""")

