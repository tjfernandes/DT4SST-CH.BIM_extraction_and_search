from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import json
from dotenv import load_dotenv

# Import logic from search.py
from search import (
    get_response, 
    build_opensearch_query, 
    execute_search, 
    SearchPlan, 
    EXTRACT_SEARCH_PLAN, 
    FINAL_RESPONSE_FORMAT
)

load_dotenv()

app = FastAPI(title="HBIM Search API")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, specify the frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []

class ChatResponse(BaseModel):
    response: str
    plan: Optional[dict] = None

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        user_input = request.message
        history = [{"role": m.role, "content": m.content} for m in request.history]
        
        # 1. Extract Search Plan
        prompt_plan = EXTRACT_SEARCH_PLAN.format(user_input=user_input)
        search_plan_message = get_response(prompt_plan, history, SearchPlan)
        search_plan = search_plan_message.parsed

        if not search_plan.needs_rag:
            response_message = get_response(user_input, history)
            response_text = response_message.content
            return ChatResponse(response=response_text, plan=search_plan.dict())
        else:
            # 2. Build and Execute Query
            os_query = build_opensearch_query(search_plan)
            hits = execute_search(os_query)
            
            if not hits:
                response_text = "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
            else:
                # 3. Generate RAG Response
                results_str = "\n".join([json.dumps(h['_source'], indent=2, ensure_ascii=False) for h in hits[:5]])
                rag_prompt = FINAL_RESPONSE_FORMAT.format(user_input=user_input, results=results_str)
                
                response_message = get_response(rag_prompt, history)
                response_text = response_message.content

            return ChatResponse(response=response_text, plan=search_plan.model_dump())
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
