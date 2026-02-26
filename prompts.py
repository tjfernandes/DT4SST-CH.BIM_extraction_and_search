import textwrap

EXTRACT_SEARCH_PLAN = textwrap.dedent("""
Extrai um plano de pesquisa estruturado a partir da pergunta do utilizador.

Regras:
- Se a pergunta requer dados do modelo BIM → needs_rag = true
- Caso contrário → needs_rag = false
- Identifica a classe IFC relevante (ex: IfcDoor, IfcStair)
- Extrai condições (campo, operador, valor), como:
  - altura → Height
  - material → Material
- Usa operadores: eq, gt, gte, lt, lte, contains

Não inventes campos. Usa apenas os que fizerem sentido.

Pergunta:
"{user_input}"
""")