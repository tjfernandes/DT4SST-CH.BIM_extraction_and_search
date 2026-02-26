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

        element_data = {
            'id': element.GlobalId,
            'project_id': project_id,
            'type': element.is_a(),
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
                'area': get_normalized_value(psets, qtos, KEYS_AREA),
                'volume': get_normalized_value(psets, qtos, KEYS_VOLUME),
                'height': get_normalized_value(psets, qtos, KEYS_HEIGHT),
                'thickness': get_normalized_value(psets, qtos, KEYS_THICKNESS),
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