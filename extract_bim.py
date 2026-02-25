import ifcopenshell
import ifcopenshell.util.element
import json
import argparse
from pathlib import Path

# Configuração do Argparse
parser = argparse.ArgumentParser(description='Extract BIM data from an IFC file and save it as JSON.')
parser.add_argument('--ifc', type=str, required=True, help='Path to the input IFC file.')
parser.add_argument('--output', type=str, required=True, help='Path to the output JSON file.')

args = parser.parse_args()
ifc_path = args.ifc
output_path = args.output

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
        return material_obj.Name

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

def extract_bim_data(ifc_file):
    ifc = ifcopenshell.open(ifc_file)
    bim_data = []
    
    elements = ifc.by_type('IfcElement')
    total = len(elements)

    for idx, element in enumerate(elements):
        if idx % 10 == 0: # Print a cada 10 para não inundar o terminal
            print(f"A processar elemento {idx + 1}/{total}: {element.Name}")
        element_data = {
            'id': element.GlobalId,
            'type': element.is_a(),
            'name': element.Name or "",
            # Chamamos a nossa nova função aqui:
            'material': get_material_name(element),
            'documents': get_associated_documents(element),
            'properties': ifcopenshell.util.element.get_psets(element, psets_only=True),
            'quantities': ifcopenshell.util.element.get_psets(element, qtos_only=True),
            'location': ifcopenshell.util.element.get_container(element).Name
        }
        
        bim_data.append(element_data)

    return bim_data

out_file = Path(output_path).resolve()
out_file.parent.mkdir(parents=True, exist_ok=True)

# Execução
print(f"A ler o ficheiro IFC: {ifc_path}")
bim_data = extract_bim_data(ifc_path)

# Escrita do JSON
with open(out_file, 'w', encoding='utf-8') as f:
    json.dump(bim_data, f, indent=4, ensure_ascii=False)

print(f"JSON guardado em: {out_file}")