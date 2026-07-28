import textwrap

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

