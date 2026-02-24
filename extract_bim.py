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

def extract_bim_data(ifc_file):
    # Abre o modelo
    ifc = ifcopenshell.open(ifc_file)
    bim_data = []

    for element in ifc.by_type('IfcElement'):
        element_data = {
            'id': element.GlobalId,
            'type': element.is_a(),
            'name': element.Name or "",
            'properties': ifcopenshell.util.element.get_psets(element, psets_only=True),
            'quantities': ifcopenshell.util.element.get_psets(element, qtos_only=True)
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