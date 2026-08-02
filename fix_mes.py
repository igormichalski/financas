import csv
from datetime import date
import dados as D

def ciclo_de(d_str: str) -> str:
    try:
        d = date.fromisoformat(d_str[:10])
    except ValueError:
        return d_str[:7]
    if d.day < 10:
        if d.month == 1:
            return f"{d.year - 1:04d}-12"
        else:
            return f"{d.year:04d}-{d.month - 1:02d}"
    return f"{d.year:04d}-{d.month:02d}"

linhas = D.ler_lancamentos()
for l in linhas:
    l["mes_ref"] = ciclo_de(l["data"])

D.gravar_lancamentos(linhas)
print("lancamentos.csv updated!")
