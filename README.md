# HBIM Search Pipeline 🏗️🔍

This project implements a complete pipeline for extracting, indexing, and intelligently searching HBIM (Heritage Building Information Modeling) data. It allows you to transform complex models (IFC) into searchable databases and interact with them through natural language using LLMs and OpenSearch.

## 🚀 Features

- **Advanced Extraction (IFC):** Converts IFC files to JSON, preserving spatial hierarchy, materials, quantities, and technical classifications.
- **Data Normalization:** Automatically identifies critical metrics (area, volume, height) regardless of the source software (Revit, ArchiCAD, etc.).
- **Geospatial and Semantic Search:** Uses **OpenSearch** to index elements, allowing for fast filtering by technical properties.
- **Intelligent Chat Interface:** A modern web interface that allows you to ask questions about the model (e.g., "How many walls are on Level 1?") using LLM-powered search plans.

---

## 🛠️ Project Structure

```text
search_pipeline/
├── backend/                # Python FastAPI Backend
│   ├── extract_bim.py      # IFC to JSON extraction script
│   ├── index_to_opensearch.py # OpenSearch indexing script
│   ├── main.py             # FastAPI Server
│   ├── search.py           # Search logic & LLM integration
│   └── environment.yml     # Conda environment definition
└── frontend/               # React + Vite + TypeScript Frontend
    ├── src/                # UI Components and logic
    └── package.json        # Frontend dependencies
```

---

## 📦 Installation and Setup

### 1. Requirements
- Python 3.10+
- Node.js & npm
- OpenSearch instance (local or cloud)
- OpenAI API Key

### 2. Backend Setup
We recommend using Conda:
```bash
cd backend
conda env create -f environment.yml
conda activate bim_data
```

Or via pip:
```bash
cd backend
pip install fastapi uvicorn ifcopenshell opensearch-py openai python-dotenv
```

### 3. Frontend Setup
```bash
cd frontend
npm install
```

### 4. Environment Variables
Create a `.env` file in the `backend/` directory:
```env
# OpenSearch
OPENSEARCH_HOST=localhost
OPENSEARCH_PORT=9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD=your_password
USE_SSL=true
VERIFY_CERTS=false
INDEX_NAME=bim_elements

# LLM
LLM_MODEL=gpt-4
OPENAI_API_KEY=your_key_here
```

---

## 📖 How to Use

### Step 1: Extract data from IFC
```bash
python backend/extract_bim.py --ifc "backend/input/model.ifc" --output "backend/output/data.json"
```

### Step 2: Index in OpenSearch
Make sure your OpenSearch instance is running, then:
```bash
python backend/index_to_opensearch.py --input "backend/output/data.json"
```

### Step 3: Start the Backend API
```bash
cd backend
uvicorn main:app --reload
```
The API will be available at `http://localhost:8000`.

### Step 4: Start the Frontend
```bash
cd frontend
npm run dev
```
Open your browser at `http://localhost:5173` to start chatting with your BIM data.

---

## 🤝 Contributions
This project was developed within the scope of data pipelines for HBIM (Heritage Building Information Modeling).
