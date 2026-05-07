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
    environment.yml         # Conda environment
  frontend/
    src/
```

## Requirements

- Python 3.10+
- Node.js and npm
- OpenSearch instance
- OpenAI API key

## Backend Setup

```bash
cd backend
conda env create -f environment.yml
conda activate bim_data
```

Or with pip:

```bash
cd backend
pip install fastapi uvicorn ifcopenshell opensearch-py openai python-dotenv sentence-transformers torch
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
LLM_LOG_OUTPUTS=true
PREPROCESS_LOG_JSONS=true

EMBEDDING_MODEL_NAME=zeroentropy/zembed-1
EMBEDDING_DIM=640
EMBEDDING_BATCH_SIZE=2
LOG_LEVEL=INFO
```

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

### 4. Start the frontend

```bash
cd frontend
npm run dev
```

Frontend URL: `http://localhost:5173`
