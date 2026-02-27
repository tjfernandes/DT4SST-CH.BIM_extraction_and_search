import textwrap

EXTRACT_SEARCH_PLAN = textwrap.dedent("""\
Extrai um plano de pesquisa estruturado (SearchPlan) a partir da pergunta do utilizador sobre modelos BIM.

[Regras de Extração]
1. needs_rag: true se a pergunta requer dados do modelo BIM, false caso contrário.
2. ifc_class: Identifica a classe IFC principal (ex: IfcDoor, IfcWall, IfcWindow, IfcStair). 
   - É CASE SENSITIVE. Mantém o PascalCase (ex: "IfcDoor", não "ifcdoor").
3. conditions: Lista de (field, op, value).
   - Fields: height, area, volume, thickness, material, name, storey.
   - Operadores: eq, approx, gt, gte, lt, lte, contains.
   - Dimensões (height, area, volume, thickness):
     - "exatamente X" → op="eq" (busca exata)
     - "X metros" / "X" (sem 'exatamente') → op="approx" (busca com margem +/- 0.5)
     - Outros (mais de, no máximo) → gt, gte, lt, lte
   - Material: Termos genéricos (ex: "madeira") → op="contains".
   - Texto: value DEVE ser uma lista curta (max 2).

[Mapeamento de Intenção]
- "mais de" / "maior que" → gt
- "pelo menos" / "no mínimo" → gte
- "menos de" / "menor que" → lt
- "no máximo" → lte
- "altura" → height
- "piso" / "nível" → storey

[Exemplos]
User: "portas de madeira com mais de 2 metros"
Plan: {{
  "needs_rag": true,
  "ifc_class": "IfcDoor",
  "conditions": [
    {{"field": "material", "op": "contains", "value": ["wood", "madeira"]}},
    {{"field": "height", "op": "gt", "value": 2.0}}
  ]
}}

User: "porta de 2 metros"
Plan: {{
  "needs_rag": true,
  "ifc_class": "IfcDoor",
  "conditions": [
    {{"field": "height", "op": "approx", "value": 2.0}}
  ]
}}

User: "janela com exatamente 1.5 metros de altura"
Plan: {{
  "needs_rag": true,
  "ifc_class": "IfcWindow",
  "conditions": [
    {{"field": "height", "op": "eq", "value": 1.5}}
  ]
}}

Pergunta do utilizador:
"{user_input}"
""")

FINAL_RESPONSE_FORMAT = textwrap.dedent("""
Resposta final:
- Pergunta do utilizador: "{user_input}"
- Resultados da pesquisa: {results}
Gera uma resposta clara e concisa para o utilizador, na lingua em que foi feita a pergunta, explicando os resultados encontrados.
Não refiras ids dos resultados.
Se não houver resultados, indica que nada foi encontrado.
""")