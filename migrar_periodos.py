#!/usr/bin/env python3
"""Migra semanas.json do modelo antigo (teto pra semana seguinte) pro novo.

No modelo antigo, dizer "meu pai mandou X" abria uma semana com teto X e arquivava
de lado o que já tinha sido gasto. Está invertido: o dinheiro que ele manda cobre o
que VOCÊ JÁ GASTOU. Este script refaz os períodos com essa conta.

Uso:  python3 migrar_periodos.py [--confirmar]
"""

import sys

import dados as D


def migrar(linhas, semanas):
    antigos = semanas.get("ciclos", [])
    if not antigos or "teto" not in antigos[0]:
        return None  # já está no formato novo

    gastos = sorted((l for l in linhas if l["conta"] == "B" and l["tipo"] == "gasto"),
                    key=lambda l: int(l["id"]))
    inicio = semanas.get("periodo_inicial", {}).get("ate")
    inicio = min((l["data"] for l in gastos), default=D.hoje().isoformat())

    novos, acumulado, corte = [], 0.0, 0
    for antigo in antigos:
        # No modelo antigo o "recebido" abria a semana. No novo, ele fecha o período
        # com o gasto que veio ANTES dele.
        recebido = float(antigo.get("recebido") or antigo.get("teto") or 0)
        ate = antigo["desde_id"]
        gasto = round(sum(l["valor"] for l in gastos
                          if corte < int(l["id"]) <= ate), 2)
        sobra = round(recebido - gasto, 2)
        acumulado = round(acumulado + sobra, 2)
        novos.append({
            "inicio": inicio,
            "desde_id": corte,
            "fechado_em": antigo["inicio"],
            "recebido": round(recebido, 2),
            "gasto": gasto,
            "sobra": sobra,
        })
        inicio, corte = antigo["inicio"], ate

    novos.append({"inicio": inicio, "desde_id": corte})
    return {"ciclos": novos, "acumulado": acumulado}


def main():
    linhas = D.ler_lancamentos()
    semanas = D.ler_semanas()
    novo = migrar(linhas, semanas)

    if novo is None:
        print("Nada a migrar: semanas.json já está no formato novo.")
        return 0

    print("ANTES:")
    for c in semanas.get("ciclos", []):
        print(f"  início {c['inicio']} · teto {D.brl(c.get('teto', 0))} · gasto contado 0")
    pi = semanas.get("periodo_inicial")
    if pi:
        print(f"  (arquivado de lado: {D.brl(pi['gasto'])} em {pi['lancamentos']} lançamentos)")

    print("\nDEPOIS:")
    for c in novo["ciclos"]:
        if c.get("fechado_em"):
            print(f"  {c['inicio']} → {c['fechado_em']} · gastou {D.brl(c['gasto'])} · "
                  f"recebeu {D.brl(c['recebido'])} · sobrou {D.brl(c['sobra'])}")
        else:
            aberto = D.gasto_no_ciclo(linhas, c)
            print(f"  {c['inicio']} → aberto · acumulando {D.brl(aberto)}")
    print(f"  guardado: {D.brl(novo['acumulado'])}")

    if "--confirmar" not in sys.argv:
        print("\nRode de novo com --confirmar pra gravar.")
        return 1

    D.gravar_semanas(novo)
    import painel
    painel.gerar(linhas, D.ler_orcamento())
    print("\nMigrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
