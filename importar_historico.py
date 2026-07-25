#!/usr/bin/env python3
"""Importa 18 meses de Nubank pro ledger e calibra os limiares com os seus números.

Roda uma vez. O painel nasce com contexto em vez de vazio, e o limiar do "destoou
do padrão" sai do seu histórico real em vez de um chute.

Uso:  python3 importar_historico.py [pasta_nubank]   (padrão: a pasta acima)
"""

import csv
import glob
import os
import re
import sys
import unicodedata
from datetime import datetime

import dados as D

ORIGEM = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(D.BASE, ".."))


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", t).strip()


def valor(x) -> float:
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace('"', "").replace(" ", "")
    if "," in s:  # formato BR: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    return float(s or 0)


def categoria(t: str) -> str:
    """Mesma lógica do analise.py, mapeada pras categorias do sistema novo."""
    if "pagamento recebido" in t:
        return "PAGAMENTO"
    # A assinatura ("Ifd*Ifood Club", R$ 5,95) é custo fixo; pedido de comida é variável.
    # Checar o clube ANTES, senão ele cai no iFood genérico.
    if "ifood club" in t or "ifood clube" in t:
        return "iFood Club"
    if t.startswith("ifd") or "ifood" in t:
        return "iFood"
    if any(k in t for k in ["fitfood", "fit food", "fetfood"]):
        return "Marmita (Fitfood)"
    if any(k in t for k in ["psicolog", "terapia"]):
        return "Psicóloga"
    if any(k in t for k in ["auto posto", "posto benjamim", "posto ", "combustiv", "ipiranga",
                            "shell", "petrobras", "oshiro", "guaicurus"]):
        return "Combustível"
    if any(k in t for k in ["leve max", "atacad", "machadao", "assai", "comper", "fort atacad",
                            "pantanal", "melo e cuenca", "comercial ramin", "supermerc",
                            "mercado", "ramin"]):
        return "Mercado/Supermercado"
    if any(k in t for k in ["airbnb", "gol linhas", "latam", "avianca", "azul lin", "viacao",
                            "immigration", "emba*", "embarque", "passagem", "hotel", "booking",
                            "decolar", "airport"]):
        return "Viagem/Passagens"
    if any(k in t for k in ["iron gym", "strikers", "academia", "odontolog", "cliniprev", "clinica",
                            "farmac", "drogar", "droga ", "pague menos", "raia", "pacheco"]):
        return "Saúde/Academia"
    if any(k in t for k in ["cambly", "curso", "udemy", "faculdade", "ingles"]):
        return "Educação/Cursos"
    if any(k in t for k in ["netflix", "spotify", "youtube", "disney", "hbo", "max ", "prime video",
                            "deezer", "globoplay", "paramount"]):
        return "Streaming"
    if any(k in t for k in ["anthropic", "claude", "openai", "chatgpt", "google", "apple.com",
                            "icloud", "microsoft", "amazon", "adobe", "github", "notion", "kabum"]):
        return "Serviços online/Tech"
    if any(k in t for k in ["acai", "hotdog", "hot dog", "lanch", "burg", "pizza", "mexican",
                            "padoca", "panela", "restaur", "sushi", "food", "acaiteria", "sorvet",
                            "cafe", "doceria", "esfiha", "pastel", "boulevard", "bento",
                            "santa pizza", "the best", "kukao", "pao dourado"]):
        return "Restaurantes/Lanches"
    if any(k in t for k in ["uber", "99app", "99*", "cabify", "taxi"]):
        return "Transporte/App"
    if any(k in t for k in ["igor roberto michalski", "agatha", "de souza"]):
        return "Pix p/ pessoas"
    return "Outros"


def ler_cartao(linhas, base):
    """faturas/Nubank_YYYY-MM-DD.csv — a data do arquivo já é o vencimento."""
    n = 0
    for arq in sorted(glob.glob(os.path.join(base, "faturas", "Nubank_*.csv"))):
        venc = re.search(r"(\d{4}-\d{2}-\d{2})", arq)
        with open(arq, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                cat = categoria(norm(r.get("title", "")))
                if cat == "PAGAMENTO":
                    continue
                v = valor(r.get("amount", 0))
                if v <= 0:
                    continue
                l = D.novo_lancamento(
                    linhas, valor=v, tipo="gasto", categoria=cat,
                    descricao=str(r.get("title", ""))[:60], data=r.get("date"),
                    pagamento="credito", origem="nubank-cartao", confianca="media",
                )
                if venc:
                    l["fatura"] = venc.group(1)
                linhas.append(l)
                n += 1
    return n


def ler_conta(linhas, base):
    """NU_*.csv — extrato da conta. Débito e Pix enviado saem no dia."""
    n = 0
    for arq in glob.glob(os.path.join(base, "NU_*.csv")):
        with open(arq, encoding="utf-8") as f:
            leitor = csv.reader(f)
            next(leitor, None)
            for row in leitor:
                if len(row) < 4:
                    continue
                d, v, _, desc = row[0], valor(row[1]), row[2], row[3]
                data = datetime.strptime(d, "%d/%m/%Y").date().isoformat()
                nd = norm(desc)
                if "pagamento de fatura" in nd or "nubank" in nd and "fatura" in nd:
                    continue  # pagar a fatura não é gasto novo: as compras já entraram

                if v > 0:
                    if "vilmar de souza" not in nd:
                        continue  # só a ajuda do pai interessa como receita
                    linhas.append(D.novo_lancamento(
                        linhas, valor=v, tipo="receita", conta="B", conta_origem="dito",
                        categoria="Outros", descricao="ajuda do pai", data=data,
                        pagamento="pix", origem="nubank-conta", confianca="alta",
                    ))
                else:
                    pag = "pix" if "pix" in nd else "debito"
                    cat = categoria(nd)
                    # Pix pra pessoa não tem nome de estabelecimento pra casar com keyword.
                    if cat == "Outros" and ("transferencia enviada" in nd or "pix" in nd):
                        cat = "Pix p/ pessoas"
                    linhas.append(D.novo_lancamento(
                        linhas, valor=-v, tipo="gasto", categoria=cat,
                        descricao=desc[:60], data=data, pagamento=pag,
                        origem="nubank-conta", confianca="media",
                    ))
                n += 1
    return n


def calibrar(linhas, orcamento):
    """Limiar = percentil 90 do valor por compra. Acima disso, o bot pergunta a conta."""
    lim = orcamento.setdefault("limiares", {})
    for cat in sorted(D.PADRAO_CONTA_B):
        vals = sorted(l["valor"] for l in linhas
                      if l["categoria"] == cat and l["tipo"] == "gasto")
        if len(vals) < 8:
            continue
        p90 = vals[int(len(vals) * 0.9)]
        lim[cat] = float(max(50, round(p90 / 10) * 10))
        print(f"  {cat}: {len(vals)} compras, mediana {D.brl(vals[len(vals) // 2])}, "
              f"limiar → {D.brl(lim[cat])}")
    return orcamento


def main():
    if not os.path.isdir(os.path.join(ORIGEM, "faturas")):
        sys.exit(f"Não achei {ORIGEM}/faturas. Passe a pasta do Nubank como argumento.")

    linhas = [l for l in D.ler_lancamentos() if not l["origem"].startswith("nubank")]
    antes = len(linhas)

    n_cartao = ler_cartao(linhas, ORIGEM)
    n_conta = ler_conta(linhas, ORIGEM)

    # Reindexa: o ledger fica ordenado por data e os ids ficam contínuos.
    linhas.sort(key=lambda l: (l["data"], l["origem"]))
    for i, l in enumerate(linhas, 1):
        l["id"] = str(i)
    D.gravar_lancamentos(linhas)

    print(f"Importado: {n_cartao} do cartão + {n_conta} da conta "
          f"(mantidos {antes} lançamentos do Telegram)")
    print("Calibrando limiares com o seu histórico:")
    D.gravar_orcamento(calibrar(linhas, D.ler_orcamento()))

    import painel
    print("Painel:", painel.gerar(linhas))


if __name__ == "__main__":
    main()
