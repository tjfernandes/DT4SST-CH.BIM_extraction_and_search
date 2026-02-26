# HBIM Search Pipeline 🏗️🔍

This project implements a complete pipeline for extracting, indexing, and intelligently searching HBIM (Heritage Building Information Modeling) data. It allows you to transform complex models (IFC) into searchable databases and interact with them through natural language using LLMs.

## 🚀 Features

- **Advanced Extraction (IFC):** Converts IFC files to JSON, preserving spatial hierarchy, materials, quantities, and technical classifications.
- **Data Normalization:** Automatically identifies critical metrics (area, volume, height) regardless of the source software (Revit, ArchiCAD, etc.).
- **Geospatial and Semantic Search:** Uses **OpenSearch** to index elements, allowing for fast filtering by technical properties.
- **Chat Interface (LLM):** Interactive terminal-based chat that allows you to ask questions about the model (e.g., "How many walls are on Level 1?") using some LLM.

## 🛠️ Project Structure

- `extract_bim.py`: Script to extract data from `.ifc` files to `.json`.
- `index_to_opensearch.py`: Loads JSON files into OpenSearch, automatically configuring mapping limits.
- `search.py`: Intelligent chat interface to interact with the data.
- `utils.py`: OpenSearch connection utilities.
- `.env`: Credentials and environment configuration.

## 📦 Installation and Setup

### 1. Requirements
- Python 3.10+
- OpenSearch instance (local or cloud)
- OpenAI API Key (for the chat)

### 2. Configure Environment
Using Conda is recommended:
```bash
conda env create -f environment.yml
conda activate bim_data
```

Or via pip:
```bash
pip install ifcopenshell opensearch-py openai python-dotenv
```

### 3. Environment Variables
Create a `.env` file in the project root with the following format:
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

## 📖 How to Use

### Step 1: Extract data from IFC
```bash
python extract_bim.py --ifc "input/model.ifc" --output "output/data.json"
```

### Step 2: Index in OpenSearch
```bash
python index_to_opensearch.py --input "output/data.json"
```

### Step 3: Talk to your data
```bash
python search.py
```

## 🤝 Contributions
This project was developed within the scope of data pipelines for HBIM (Heritage Building Information Modeling). 
