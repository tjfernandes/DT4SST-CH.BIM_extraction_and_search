import textwrap

# ── Tabela de referência IFC ──────────────────────────────────────────
# Tabela explícita de termos → classes IFC, para que modelos pequenos
# consigam mapear sem conhecimento prévio de IFC4.
IFC_CLASS_TABLE = """\
| Termos (PT / EN)                                           | ifc_class               |
|------------------------------------------------------------|-------------------------|
| porta, portas, door, doors                                 | IfcDoor                 |
| janela, janelas, window, windows                           | IfcWindow               |
| parede, paredes, wall, walls, muro                         | IfcWall                 |
| laje, lajes, pavimento, slab, floor slab                   | IfcSlab                 |
| pilar, pilares, coluna, colunas, column, columns           | IfcColumn               |
| viga, vigas, beam, beams                                   | IfcBeam                 |
| escada, escadas, stair, stairs, staircase                  | IfcStair                |
| telhado, cobertura, roof                                   | IfcRoof                 |
| rampa, rampas, ramp, ramps                                 | IfcRamp                 |
| fachada cortina, curtain wall                              | IfcCurtainWall          |
| guarda, guardas, corrimão, railing, handrail               | IfcRailing              |
| mobiliário, móvel, móveis, furniture, furnishing           | IfcFurnishingElement    |
| placa, placas, plate                                       | IfcPlate                |
| membro, member                                             | IfcMember               |
| abertura, aberturas, opening                               | IfcOpeningElement       |
| revestimento, revestimentos, covering                      | IfcCovering             |
| genérico, proxy, artefacto, artefactos, artifact           | IfcBuildingElementProxy |
| tubo, tubagem, pipe, pipe segment                          | IfcFlowSegment          |
| válvula, controlador, valve, flow controller               | IfcFlowController       |
| torneira, sanita, terminal, flow terminal                  | IfcFlowTerminal         |
| acessório, fitting, flow fitting                           | IfcFlowFitting          |
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

[Campos de saída]
1. search_strategy – "chat", "structured", "semantic" ou "aggregation"
2. semantic_query – frase curta em inglês descritiva do que o utilizador procura (apenas quando search_strategy="semantic"), caso contrário null

[Regras]
- Usa "chat" se a pergunta NÃO é sobre elementos do modelo (ex: "olá", "o que é o IFC?", "como estás?").
- Usa "aggregation" APENAS se o utilizador pede explicitamente contagens, valores distintos ou estatísticas (ex: "quantas", "quais materiais existem", "que tipos há"). Palavras como "listar", "mostrar", "ver" referem-se a elementos individuais → usa "structured" ou "semantic".
- Se a pergunta é um follow-up vago (ex: "consegues listá-las?", "mostra-me", "sim"), interpreta no contexto do histórico. Se o contexto anterior era sobre elementos, usa "structured".
- Usa "structured" se TODOS os critérios podem ser expressos como filtros exatos (classe, dimensões numéricas), ou se o utilizador quer VER elementos individuais.
- Usa "semantic" se a pergunta contém termos vagos, descritivos, conceptuais, funcionais ou relações que não se mapeiam a filtros exatos.
- Para semantic_query: cria uma frase curta em inglês que capture a intenção semântica, incluindo o tipo de elemento se mencionado.
- Devolve apenas um objeto JSON válido.
                                  
[Exemplos]
Pergunta: "olá, como estás?"
→ {{"search_strategy": "chat", "semantic_query": null}}

Pergunta: "mostra-me as portas do piso 1"
→ {{"search_strategy": "structured", "semantic_query": null}}

Pergunta: "paredes de betão com mais de 3 metros"
→ {{"search_strategy": "structured", "semantic_query": null}}

Pergunta: "mostra-me todos os elementos do piso 2"
→ {{"search_strategy": "structured", "semantic_query": null}}

Pergunta: "artefactos de calcário"
→ {{"search_strategy": "structured", "semantic_query": null}}

Pergunta: "vigas com mais de 5 metros"
→ {{"search_strategy": "structured", "semantic_query": null}}

Pergunta: "lista todos os materiais das paredes"
→ {{"search_strategy": "aggregation", "semantic_query": null}}

Pergunta: "quais são os pisos do edifício?"
→ {{"search_strategy": "aggregation", "semantic_query": null}}

Pergunta: "quantas portas existem por piso?"
→ {{"search_strategy": "aggregation", "semantic_query": null}}

Pergunta: "que tipos de elementos existem no modelo?"
→ {{"search_strategy": "aggregation", "semantic_query": null}}

Pergunta: "quantos elementos tem o modelo?"
→ {{"search_strategy": "aggregation", "semantic_query": null}}

Pergunta: "lista as paredes"
→ {{"search_strategy": "structured", "semantic_query": null}}
                                  
Pergunta: "Que elementos contêm documentos associados?"
→ {{"search_strategy": "semantic", "semantic_query": "has documents"}}
                                  
Pergunta: "Que elementos contêm classificações associadas?"
→ {{"search_strategy": "semantic", "semantic_query": "has classifications"}}                                  

Pergunta: "elementos estruturais do edifício"
→ {{"search_strategy": "semantic", "semantic_query": "structural building elements"}}

Pergunta: "tudo relacionado com a fachada"
→ {{"search_strategy": "semantic", "semantic_query": "facade related elements"}}

Pergunta: "paredes com anomalias ou patologias"
→ {{"search_strategy": "semantic", "semantic_query": "walls with anomalies or pathologies"}}

Pergunta: "componentes de proteção contra incêndio"
→ {{"search_strategy": "semantic", "semantic_query": "fire protection components"}}

Pergunta: "o que suporta o telhado?"
→ {{"search_strategy": "semantic", "semantic_query": "roof support elements"}}

Pergunta: "elementos decorativos da fachada principal"
→ {{"search_strategy": "semantic", "semantic_query": "decorative elements of the main facade"}}

Pergunta: "elementos mais antigos do modelo"
→ {{"search_strategy": "semantic", "semantic_query": "oldest elements in the model"}}

Pergunta do utilizador:
"{user_input}"
""")

# ── Passo 1b: Extrair classe IFC ──────────────────────────────────────
EXTRACT_IFC_CLASS = textwrap.dedent("""\
Identifica a classe IFC mencionada na pergunta do utilizador.

[Tabela de Classes IFC]
{ifc_table}

[Campos de saída]
1. ifc_class – a classe IFC do objeto mencionado (da tabela acima), ou null

[Regras]
- Procura na pergunta palavras que correspondam à coluna "Termos" da tabela acima.
- Se encontrares correspondência, copia o valor EXATO da coluna "ifc_class" (PascalCase, case-sensitive).
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
2. project_id: extrai se o utilizador mencionar um ID de projeto específico. Usa null se não mencionado.
3. project_name: extrai se o utilizador mencionar o nome de um projeto específico. Usa null se não mencionado.

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

FINAL_RESPONSE_FORMAT = textwrap.dedent("""
- Pergunta do utilizador: "{user_input}"
- A mostrar resultados {showing} de {total} no total
- Resultados da pesquisa:
{results}

Gera uma resposta clara e concisa para o utilizador, na lingua em que foi feita a pergunta, explicando os resultados encontrados.
Indica que estás a mostrar os resultados {showing} de {total} no total, apenas se não forem iguais.
Não refiras ids dos resultados.
Se não houver resultados, indica que nada foi encontrado.
IMPORTANTE: Sempre que apresentares um URL ou caminho de ficheiro (ex: https://... ou docs/...), formata-o OBRIGATORIAMENTE como hiperligação Markdown: [texto descritivo](url_ou_caminho). NUNCA uses backticks nem texto simples para URLs ou caminhos.
""")

# ── Filtro LLM de relevância (por resultado individual) ───────────────
FILTER_SINGLE_RESULT = textwrap.dedent("""\
O utilizador perguntou: "{user_input}"

Este resultado foi devolvido pela pesquisa:
{result}

Este resultado é relevante para a pergunta do utilizador?
Retorna JSON: {{"relevant": true}} ou {{"relevant": false}}

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
- project_name    → nome do projeto (ex: "Mosteiro de Santa Clara a Velha")

[Campo de saída]
1. agg_field – o campo a agregar (um dos acima)

[Regras]
- Se o utilizador pede "quantos X existem?" ou "número de X", usa agg_field="count".
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
""")

AGGREGATION_RESPONSE_FORMAT = textwrap.dedent("""\
Pergunta do utilizador: "{user_input}"

Resultados da agregação ({agg_field}):
{results}

Gera uma resposta clara e concisa para o utilizador, na lingua em que foi feita a pergunta, apresentando os resultados da agregação.
Se houver contagens, apresenta-as. Formata como lista ou tabela Markdown se adequado.
""")