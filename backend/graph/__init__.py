"""HBIM-079 — the project-owned canonical graph intermediate representation.

Nothing in this package imports IfcOpenShell, OpenSearch, FastAPI, a model
client or Neo4j. Only ``graph.adapters.ifcopenshell_adapter`` touches an IFC
library, and it does so lazily inside the call. Importing this package performs
no I/O, opens no socket and starts no subprocess.
"""
