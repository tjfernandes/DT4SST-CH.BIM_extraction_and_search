import json
from opensearchpy import OpenSearch, helpers
from openai import OpenAI
import os
from dotenv import load_dotenv
from prompts import EXTRACT_SEARCH_PLAN
import textwrap
from pydantic import BaseModel
from typing import List, Optional, Union


class Condition(BaseModel):
    field: str              # ex: "Height", "Material"
    op: str                 # "eq", "gt", "gte", "lt", "lte", "contains"
    value: Union[float, str, bool]


class SearchPlan(BaseModel):
    needs_rag: bool
    ifc_class: Optional[str] = None      # "IfcDoor", "IfcStair"
    conditions: List[Condition] = []
    top_k: int = 20

# Carregar variáveis de ambiente do ficheiro .env
load_dotenv()

gpt_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_response(prompt, history=[], response_format={"type": "text"}):
    messages = [{"role": "system", "content": "És um assistente especializado em dados BIM (Building Information Modeling). Ajuda o utilizador a explorar os seus modelos."}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    response = gpt_client.chat.completions.parse(
        model=os.getenv("LLM_MODEL", "gpt-4"),
        messages=messages,
        response_format=response_format
    )
    
    return response.choices[0].message

def chat():
    print("--- Chat BIM (LLM) iniciado. Digite 'sair' para encerrar. ---")
    history = []
    
    while True:
        user_input = input("\nTu: ")
        
        if user_input.lower() in ['sair', 'exit', 'quit']:
            print("A encerrar chat...")
            break
            
        if not user_input.strip():
            continue
            
        try:

            search_plan = get_response(EXTRACT_SEARCH_PLAN.format(user_input=user_input), history, SearchPlan)

            print(f"\nAssistente: {search_plan}")
            
            # Atualizar histórico
            history.append({"role": "user", "content": user_input})
            history.append({"role": "assistant", "content": search_plan})
            
            # Limitar histórico para não exceder contexto
            if len(history) > 20:
                history = history[-20:]
                
        except Exception as e:
            print(f"\nErro ao processar pedido: {e}")

if __name__ == "__main__":
    chat()


