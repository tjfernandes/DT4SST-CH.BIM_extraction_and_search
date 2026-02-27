import ifcopenshell
import ifcopenshell.util.element
import json
import argparse
from pathlib import Path

# CONSTANTES DE NORMALIZAÇÃO
KEYS_AREA = ['NetArea', 'GrossArea', 'Area', 'Área', 'Area_Value']
KEYS_VOLUME = ['NetVolume', 'GrossVolume', 'Volume', 'Volume_Value']
KEYS_HEIGHT = ['Height', 'Altura', 'UnboundedHeight', 'L']
KEYS_THICKNESS = ['Width', 'Thickness', 'Espessura', 'Largura']
#KEYS_DATE = ['InterventionDate', 'LastAssessmentDate', 'SurveyDate', 'Data']

# Configuração do Argparse
parser = argparse.ArgumentParser(description='Extract BIM data from an IFC file and save it as JSON.')
parser.add_argument('--ifc', type=str, required=True, help='Path to the input IFC file.')
parser.add_argument('--output', type=str, required=True, help='Path to the output JSON file.')
parser.add_argument('--project-id', type=str, help='Project GUID to associate with this BIM data. If not provided, it will be extracted from the IFC file.')

args = parser.parse_args()
ifc_path = args.ifc
output_path = args.output
project_id_arg = args.project_id

def _si_prefix_factor(prefix: str | None) -> float:
    # IFC prefixes comuns
    return {
        None: 1.0,
        "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6, "KILO": 1e3,
        "HECTO": 1e2, "DECA": 1e1,
        "DECI": 1e-1, "CENTI": 1e-2, "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9,
        "PICO": 1e-12, "FEMTO": 1e-15, "ATTO": 1e-18,
    }.get(prefix, 1.0)


def _length_unit_to_m_factor(ifc) -> float:
    """
    Devolve fator multiplicativo para converter unidades de comprimento do IFC -> metros.
    Ex: se IFC estiver em mm, devolve 0.001.
    """
    projects = ifc.by_type("IfcProject")
    if not projects:
        return 1.0

    units = getattr(projects[0], "UnitsInContext", None)
    if not units:
        return 1.0

    unit_assignment = getattr(units, "Units", None)
    if not unit_assignment:
        return 1.0

    # procurar LENGTHUNIT
    for u in unit_assignment:
        try:
            unit_type = getattr(u, "UnitType", None)
            if unit_type != "LENGTHUNIT":
                continue

            # Caso 1: IfcSIUnit (mais comum)
            if u.is_a("IfcSIUnit"):
                # Name costuma ser METRE e Prefix MILLI para mm
                if getattr(u, "Name", None) == "METRE":
                    return _si_prefix_factor(getattr(u, "Prefix", None))
                # Se aparecer outra coisa, assume 1
                return 1.0

            # Caso 2: IfcConversionBasedUnit (menos comum)
            if u.is_a("IfcConversionBasedUnit"):
                # ConversionFactor é IfcMeasureWithUnit
                cf = getattr(u, "ConversionFactor", None)
                if not cf:
                    return 1.0
                # ValueComponent tem valor numérico (ex: 0.3048) e UnitComponent diz para que SI unit é
                val_comp = getattr(cf, "ValueComponent", None)
                unit_comp = getattr(cf, "UnitComponent", None)

                # tenta extrair número
                factor = None
                if val_comp is not None:
                    # alguns vêm como objetos com wrappedValue
                    factor = getattr(val_comp, "wrappedValue", None)
                    if factor is None and isinstance(val_comp, (int, float)):
                        factor = float(val_comp)

                # se unit_comp for SI METRE, o factor já está em metros
                if factor is not None:
                    if unit_comp and unit_comp.is_a("IfcSIUnit") and getattr(unit_comp, "Name", None) == "METRE":
                        # pode ter prefix no UnitComponent (raro)
                        return float(factor) * _si_prefix_factor(getattr(unit_comp, "Prefix", None))
                    return float(factor)

                return 1.0

        except Exception:
            continue

    return 1.0


def _to_float(x):
    try:
        return float(getattr(x, "wrappedValue", x))
    except Exception:
        return None

def get_normalized_value(psets, qtos, keys):
    """Procura um valor numa lista de chaves tanto nos Psets como nos Qtos."""
    
    # Garantir que psets e qtos são dicionários (ou listas de dicionários)
    # Se forem tuplas/listas, iteramos diretamente. Se for dict, usamos .values()
    
    def search_in_collection(collection):
        if not collection:
            return None
        
        # Se for um dicionário (que é o esperado do get_psets moderno)
        if isinstance(collection, dict):
            source = collection.values()
        # Se for uma tupla ou lista (o que causou o seu erro)
        else:
            source = collection
            
        for data_set in source:
            # data_set aqui é o dicionário de propriedades (ex: {'NetArea': 10.5})
            if isinstance(data_set, dict):
                for key in keys:
                    if key in data_set:
                        return data_set[key]
        return None

    # 1. Tenta primeiro nas Quantities
    val = search_in_collection(qtos)
    if val is not None: return val
    
    # 2. Fallback para Property Sets
    return search_in_collection(psets)

def get_material_name(element):
    """
    Extrai o nome do material de forma amigável para JSON.
    Lida com materiais simples, listas e camadas.
    """
    material_obj = ifcopenshell.util.element.get_material(element)
    
    if not material_obj:
        return None

    # Caso 1: É um material simples (IfcMaterial)
    if material_obj.is_a('IfcMaterial'):
        return [material_obj.Name]

    # Caso 2: É uma lista/constituintes (IfcMaterialConstituentSet)
    if material_obj.is_a('IfcMaterialConstituentSet'):
        return [c.Material.Name for c in material_obj.MaterialConstituents if c.Material]

    # Caso 3: São camadas (IfcMaterialLayerSet / Usage)
    if hasattr(material_obj, 'ForLayerSet'):
        layer_set = material_obj.ForLayerSet
        return [layer.Material.Name for layer in layer_set.MaterialLayers if layer.Material]
    
    # Caso 4: IfcMaterialList
    if material_obj.is_a('IfcMaterialList'):
        return [m.Name for m in material_obj.Materials]

    # Fallback: tenta retornar o nome se o atributo existir, senão o tipo
    return getattr(material_obj, "Name", material_obj.is_a())


def get_associated_documents(element):
    """
    Procura por documentos, PDFs e URIs associados ao elemento
    via IfcRelAssociatesDocument.
    """
    documents = []
    
    # HasAssociations guarda links para materiais, classificações e documentos
    if not hasattr(element, "HasAssociations"):
        return documents

    for assoc in element.HasAssociations:
        if assoc.is_a("IfcRelAssociatesDocument"):
            # O 'RelatingDocument' pode ser um IfcDocumentInformation ou IfcDocumentReference
            doc_ref = assoc.RelatingDocument
            
            doc_info = {
                "name": doc_ref.Name or "",
                "description": getattr(doc_ref, "Description", ""),
                # No IFC, o URI/Caminho fica no atributo Location
                "location": getattr(doc_ref, "Location", ""), 
                "id": getattr(doc_ref, "Identification", "") # Código do documento
            }
            documents.append(doc_info)
            
    return documents

def get_classifications(element):
    """Extrai códigos de classificação associados (ex: WBS, tabelas de património)."""
    classificacoes = []
    if not hasattr(element, "HasAssociations"):
        return classificacoes
    
    for assoc in element.HasAssociations:
        if assoc.is_a("IfcRelAssociatesClassification"):
            cl = assoc.RelatingClassification
            classificacoes.append({
                "source": getattr(cl.ReferencedSource, "Name", "N/A") if hasattr(cl, "ReferencedSource") else "N/A",
                "code": getattr(cl, "Identification", ""),
                "name": getattr(cl, "Name", "")
            })
    return classificacoes

def sanitize_keys(d):
    """Recursively replaces dots in dictionary keys with underscores for OpenSearch compatibility."""
    if not isinstance(d, dict):
        return d
    new_dict = {}
    for k, v in d.items():
        new_key = k.replace('.', '_')
        if isinstance(v, dict):
            new_dict[new_key] = sanitize_keys(v)
        elif isinstance(v, list):
            new_dict[new_key] = [sanitize_keys(i) if isinstance(i, dict) else i for i in v]
        else:
            new_dict[new_key] = v
    return new_dict

def extract_bim_data(ifc_file, project_id=None):
    ifc = ifcopenshell.open(ifc_file)

    length_to_m = _length_unit_to_m_factor(ifc)  # <-- AQUI
    area_to_m2 = length_to_m ** 2
    vol_to_m3 = length_to_m ** 3
    
    # Se não foi passado um project_id via CLI, tenta extrair do ficheiro IFC
    if not project_id:
        projects = ifc.by_type('IfcProject')
        if projects:
            project_id = projects[0].GlobalId
        else:
            project_id = "Unknown_Project"

    bim_data = []
    
    elements = ifc.by_type('IfcElement')

    for idx, element in enumerate(elements):
        # Obter o Contentor Espacial (Piso)
        container = ifcopenshell.util.element.get_container(element)
        
        # Obter o Agregado (Pai técnico, ex: a Escada que contém o degrau)
        parent = ifcopenshell.util.element.get_aggregate(element)

        # Obter Psets e Qtos e sanitizar chaves (substituir . por _)
        psets = sanitize_keys(ifcopenshell.util.element.get_psets(element, psets_only=True))
        qtos = sanitize_keys(ifcopenshell.util.element.get_psets(element, qtos_only=True))

        raw_area = _to_float(get_normalized_value(psets, qtos, KEYS_AREA))
        raw_vol = _to_float(get_normalized_value(psets, qtos, KEYS_VOLUME))
        raw_h = _to_float(get_normalized_value(psets, qtos, KEYS_HEIGHT))
        raw_th = _to_float(get_normalized_value(psets, qtos, KEYS_THICKNESS))

        element_data = {
            'id': element.GlobalId,
            'project_id': project_id,
            'ifc_class': element.is_a(),
            'name': element.Name or "",
            
            # PESQUISA ESPACIAL
            'spatial_hierarchy': {
                'storey_name': container.Name if (container and hasattr(container, 'Name')) else "Exterior/Unassigned",
                'storey_id': container.GlobalId if (container and hasattr(container, 'GlobalId')) else None,
                'parent_element_id': parent.GlobalId if parent else None
            },
            
            # DADOS TÉCNICOS
            'material': get_material_name(element),
            'documents': get_associated_documents(element),
            'classifications': get_classifications(element),
            
            # PROPRIEDADES (Heritage Psets e Quantities)
            'properties': psets,
            'quantities': qtos,

            # Campos Normalizados (Para Pesquisa Rápida)
            'metrics': {
                'area': (raw_area * area_to_m2) if raw_area is not None else None,         # m²
                'volume': (raw_vol * vol_to_m3) if raw_vol is not None else None,         # m³
                'height': (raw_h * length_to_m) if raw_h is not None else None,           # m
                'thickness': (raw_th * length_to_m) if raw_th is not None else None,      # m
            },
        }
        
        bim_data.append(element_data)

    return bim_data

out_file = Path(output_path).resolve()
out_file.parent.mkdir(parents=True, exist_ok=True)

# Execução
print(f"A ler o ficheiro IFC: {ifc_path}")
bim_data = extract_bim_data(ifc_path, project_id=project_id_arg)

# Escrita do JSON
with open(out_file, 'w', encoding='utf-8') as f:
    json.dump(bim_data, f, indent=4, ensure_ascii=False)

print(f"JSON guardado em: {out_file}")