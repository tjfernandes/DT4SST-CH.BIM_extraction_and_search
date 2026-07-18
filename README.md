# HBIM Search Pipeline

This project provides an end-to-end HBIM pipeline:

- IFC extraction to JSON
- OpenSearch indexing with semantic embeddings
- FastAPI chat/search API for BIM exploration
- React frontend client

## Project Structure

```text
search_pipeline/
  backend/
    api/                    # Online search API
      main.py               # FastAPI entrypoint
      search.py             # Search + aggregation logic
      prompts.py            # LLM prompt templates
    ingestion/              # Offline data preparation
      extract_bim.py        # IFC -> JSON
      index_to_opensearch.py# JSON -> OpenSearch
    shared/                 # Shared infra/config
      config.py             # Env + runtime settings
      opensearch.py         # OpenSearch client factory
    input/                  # Sample IFC inputs (gitignored by default)
    output/                 # Extracted JSON outputs (gitignored by default)
    requirements*.txt       # Runtime / ML / dev dependencies
  frontend/
    src/
```

Development workflow (tests, quality checks, local services):
see [docs/development/LOCAL_SETUP.md](docs/development/LOCAL_SETUP.md).

## Requirements

- Python 3.10+
- Node.js and npm
- OpenSearch instance
- LLM API key (openai compatible)

## Backend Setup

The operative conda environment is `hbim-rag` (Python 3.10):

```bash
conda run -n hbim-rag python -m pip install -r backend/requirements.txt
# ML/embedding stack (multi-GB; needed only for indexing / semantic queries):
conda run -n hbim-rag python -m pip install -r backend/requirements-ml.txt
# Development and test tooling:
conda run -n hbim-rag python -m pip install -r backend/requirements-dev.txt
```

## Frontend Setup

```bash
cd frontend
npm install
```

## Environment Variables

Create `.env` in `backend/`:

```env
OPENSEARCH_HOST=localhost
OPENSEARCH_PORT=9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=your_password
USE_SSL=true
VERIFY_CERTS=false
SSL_SHOW_WARN=false
OPENSEARCH_INDEX=bim_elements

LLM_MODEL=your_model
LLM_API_KEY=your_key_here
# Optional for OpenAI-compatible providers. Use the API root, not /chat/completions.
LLM_BASE_URL=https://api.example.com/v1
LLM_LOG_PROMPTS=false
LLM_LOG_OUTPUTS=true
PREPROCESS_LOG_JSONS=true

EMBEDDING_MODEL_NAME=zeroentropy/zembed-1
EMBEDDING_DIM=640
EMBEDDING_BATCH_SIZE=2
LOG_LEVEL=INFO

# API authentication and hardening (HBIM-003)
# Fail closed: with auth enabled, API_KEYS must be a non-empty JSON array or
# comma-separated list. Set API_AUTH_ENABLED=false only deliberately (dev).
API_AUTH_ENABLED=true
API_KEYS=your_api_key_here
METRICS_PUBLIC=false
CORS_ALLOW_ORIGINS=http://localhost:5173
CORS_ALLOW_CREDENTIALS=false
LOG_FORMAT=json
```

Create `.env` in `frontend/` (see `frontend/.env.example`):

```env
VITE_API_KEY=your_client_key_here
# Optional; defaults to http://localhost:8000
# VITE_API_BASE_URL=http://localhost:8000
```

> `VITE_*` variables are inlined into the browser bundle at build time.
> `VITE_API_KEY` is a **browser-visible client key**, not a confidential
> server secret: provision it separately from any server-side credential and
> rotate it independently.

## How to Run

### 1. Extract IFC data

```bash
cd backend
python -m ingestion.extract_bim --ifc "input/model.ifc" --output "output/data.json"
```

### 2. Index JSON in OpenSearch

```bash
cd backend
python -m ingestion.index_to_opensearch --input "output/data.json"
```

### 3. Start the API

```bash
cd backend
uvicorn api.main:app --reload
```

API URL: `http://localhost:8000`

Endpoints: `POST /chat` (requires the `X-API-Key` header when authentication
is enabled), `GET /healthz` (liveness), `GET /readyz` (readiness),
`GET /metrics` (Prometheus; requires the API key unless `METRICS_PUBLIC=true`).
`GET /health` is a **deprecated** alias of `/healthz` kept for compatibility.

### 4. Start the frontend

```bash
cd frontend
npm run dev
```

Frontend URL: `http://localhost:5173`
