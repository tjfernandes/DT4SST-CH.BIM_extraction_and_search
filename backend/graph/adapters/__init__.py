"""HBIM-079 §36 — candidate adapters behind the project-owned IR boundary.

Only ``ifcopenshell_adapter`` may touch an IFC library, and only lazily inside
its ``extract`` call. No adapter for candidates B or C exists: both are
``preflight_ineligible`` under the frozen Session-1 audit and are never
installed, imported or executed.
"""
