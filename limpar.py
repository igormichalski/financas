#!/usr/bin/env python3
"""Zera o ledger e recomeça do zero.

Preserva de propósito:
- os limiares já calibrados em orcamento.json (o "combustível acima de X eu pergunto")
- o offset do Telegram, pra não reprocessar mensagem velha
- os valores esperados e os recorrentes

Uso:  python3 limpar.py --confirmar
"""

import os
import sys

import dados as D
import painel


def main():
    if "--confirmar" not in sys.argv:
        linhas = D.ler_lancamentos()
        print(f"Isso apaga {len(linhas)} lançamento(s) do ledger.")
        print("Preserva: limiares calibrados, valores esperados, recorrentes e o offset.")
        print("\nRode de novo com --confirmar se for isso mesmo.")
        return 1

    D.gravar_lancamentos([])
    D.gravar_semanas({"ciclos": []})
    D.gravar_pendencias({"abertas": []})
    D.gravar_fila({"pendentes": []})

    # Some com o revisar.csv: ele só tinha resto do histórico importado.
    if os.path.exists(D.REVISAR):
        os.remove(D.REVISAR)

    # Mantém o offset (não reprocessa mensagem antiga) mas libera os avisos,
    # senão a cobrança de recorrente só voltaria mês que vem.
    state = D.ler_state()
    state["avisos"] = {}
    state["erros"] = {}
    D.gravar_state(state)

    painel.gerar([], D.ler_orcamento())
    print("Ledger zerado. O painel nasce limpo e só cresce com o que você falar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
