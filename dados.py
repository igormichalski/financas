"""Núcleo de dados: ledger, orçamento, ciclo de fatura e as duas carteiras.

Conta A = seu salário (teto mensal).
Conta B = ajuda do pai (teto semanal, reseta quando ele manda).
Os dois orçamentos nunca se somam.
"""

import csv
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("America/Sao_Paulo")

LANCAMENTOS = os.path.join(BASE, "lancamentos.csv")
ORCAMENTO = os.path.join(BASE, "orcamento.json")
RECORRENTES = os.path.join(BASE, "recorrentes.json")
SEMANAS = os.path.join(BASE, "semanas.json")
PENDENCIAS = os.path.join(BASE, "pendencias.json")
STATE = os.path.join(BASE, "state.json")
REVISAR = os.path.join(BASE, "revisar.csv")
FILA = os.path.join(BASE, "fila.json")

# Bots do Telegram só baixam arquivo até 20 MB; o Gemini aceita ~20 MB inline.
LIMITE_AUDIO = 18 * 1024 * 1024
# Depois disso o item sai da fila pra não travar tudo pra sempre.
MAX_TENTATIVAS = 3

# Cartão Nubank: fecha dia 15, vence dia 22.
FECHAMENTO = 15
VENCIMENTO = 22

CAMPOS = [
    "id", "ts", "data", "tipo", "valor", "conta", "categoria", "descricao",
    "pagamento", "fatura", "origem", "confianca", "conta_origem",
    "transcricao", "msg_id",
]

CATEGORIAS = [
    "iFood",          # pedido de comida — variável
    "iFood Club",     # a assinatura mensal — fixa. São coisas diferentes.
    "Marmita (Fitfood)",
    "Combustível",
    "Mercado/Supermercado",
    "Restaurantes/Lanches",
    "Transporte/App",
    "Saúde/Academia",
    "Psicóloga",
    "Streaming",
    "Serviços online/Tech",
    "Educação/Cursos",
    "Viagem/Passagens",
    "Pix p/ pessoas",
    "Outros",
]

# Categorias que o pai banca — caem na conta B quando você não diz nada.
PADRAO_CONTA_B = {"Mercado/Supermercado", "Combustível", "Marmita (Fitfood)"}


def hoje() -> date:
    return datetime.now(TZ).date()


def agora() -> datetime:
    return datetime.now(TZ)


# ---------------------------------------------------------------- fatura


def fatura_de(d: date) -> str:
    """Em qual fatura a compra cai. Ciclo dia 16 → dia 15.

    Compra em 10/jul fecha 15/jul e vence 22/jul.
    Compra em 16/jul fecha 15/ago e vence 22/ago — um dia depois, um mês a mais.
    """
    if d.day <= FECHAMENTO:
        ano, mes = d.year, d.month
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f"{ano:04d}-{mes:02d}-{VENCIMENTO:02d}"


def fatura_aberta(ref: date | None = None) -> str:
    """A fatura que ainda está acumulando compras hoje."""
    return fatura_de(ref or hoje())


def fecha_em(ref: date | None = None) -> tuple[date, int]:
    """Data do próximo fechamento e quantos dias faltam."""
    d = ref or hoje()
    if d.day <= FECHAMENTO:
        alvo = d.replace(day=FECHAMENTO)
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        alvo = date(ano, mes, FECHAMENTO)
    return alvo, (alvo - d).days


def vence_em(ref: date | None = None) -> tuple[date, int]:
    """Data do próximo vencimento e quantos dias faltam."""
    d = ref or hoje()
    if d.day <= VENCIMENTO:
        alvo = d.replace(day=VENCIMENTO)
    else:
        ano, mes = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        alvo = date(ano, mes, VENCIMENTO)
    return alvo, (alvo - d).days


# ---------------------------------------------------------------- json


def _ler_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Arquivo corrompido não pode derrubar o sistema inteiro: guarda o
        # estragado pra perícia e segue com o padrão.
        os.replace(caminho, caminho + ".corrompido")
        return padrao


def _gravar_json(caminho, dados):
    """Escreve num temporário e troca de uma vez: job morto no meio não corrompe arquivo."""
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)


def ler_orcamento():
    return _ler_json(ORCAMENTO, {})


def gravar_orcamento(o):
    _gravar_json(ORCAMENTO, o)


def ler_recorrentes():
    return _ler_json(RECORRENTES, {"itens": []})


def gravar_recorrentes(r):
    _gravar_json(RECORRENTES, r)


def ler_semanas():
    return _ler_json(SEMANAS, {"ciclos": []})


def gravar_semanas(s):
    _gravar_json(SEMANAS, s)


def ler_pendencias():
    return _ler_json(PENDENCIAS, {"abertas": []})


def gravar_pendencias(p):
    _gravar_json(PENDENCIAS, p)


def ler_state():
    return _ler_json(STATE, {"offset": 0, "ultimo_run": None, "avisos": {}})


def gravar_state(s):
    _gravar_json(STATE, s)


def ler_fila():
    """Mensagens baixadas do Telegram e ainda não processadas.

    Existe porque o Telegram só guarda mensagem não lida por 24h. Se o Gemini ficar
    fora do ar dois dias, o que estiver aqui (versionado no git) sobrevive.
    """
    return _ler_json(FILA, {"pendentes": []})


def gravar_fila(f):
    _gravar_json(FILA, f)


# ---------------------------------------------------------------- ledger


def ler_lancamentos() -> list[dict]:
    if not os.path.exists(LANCAMENTOS):
        return []
    with open(LANCAMENTOS, encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    boas = []
    for l in linhas:
        try:
            l["valor"] = float(l["valor"] or 0)
        except (TypeError, ValueError):
            continue  # linha editada à mão e quebrada não derruba o run inteiro
        for c in CAMPOS:
            l.setdefault(c, "")
        boas.append(l)
    return boas


def gravar_lancamentos(linhas: list[dict]) -> None:
    linhas = sorted(linhas, key=lambda l: (l["data"], l["id"]))
    tmp = LANCAMENTOS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        w.writeheader()
        for l in linhas:
            w.writerow({c: l.get(c, "") for c in CAMPOS})
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LANCAMENTOS)  # o ledger nunca fica pela metade


def proximo_id(linhas: list[dict]) -> int:
    return max((int(l["id"]) for l in linhas if str(l["id"]).isdigit()), default=0) + 1


def novo_lancamento(linhas: list[dict], **campos) -> dict:
    """Monta um lançamento completo, derivando fatura e conta quando faltarem."""
    d = campos.get("data") or hoje().isoformat()
    categoria = campos.get("categoria") or "Outros"
    pagamento = campos.get("pagamento") or "nao_informado"

    conta = campos.get("conta")
    conta_origem = campos.get("conta_origem") or "padrao_categoria"
    if not conta:
        conta = "B" if categoria in PADRAO_CONTA_B else "A"
        conta_origem = "padrao_categoria"

    # Fatura só existe pra crédito: débito, Pix e dinheiro saem no dia.
    fatura = fatura_de(date.fromisoformat(d)) if pagamento == "credito" else ""

    return {
        "id": str(campos.get("id") or proximo_id(linhas)),
        "ts": campos.get("ts") or agora().isoformat(timespec="seconds"),
        "data": d,
        "tipo": campos.get("tipo") or "gasto",
        "valor": round(float(campos.get("valor") or 0), 2),
        "conta": conta,
        "categoria": categoria,
        "descricao": campos.get("descricao") or "",
        "pagamento": pagamento,
        "fatura": fatura,
        "origem": campos.get("origem") or "telegram",
        "confianca": campos.get("confianca") or "alta",
        "conta_origem": conta_origem,
        "transcricao": campos.get("transcricao") or "",
        "msg_id": str(campos.get("msg_id") or ""),
    }


def registrar_revisao(motivo: str, transcricao: str) -> None:
    novo = not os.path.exists(REVISAR)
    with open(REVISAR, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["ts", "motivo", "transcricao"])
        w.writerow([agora().isoformat(timespec="seconds"), motivo, transcricao])


# ---------------------------------------------------------------- conta B


def ciclo_aberto(semanas=None):
    """O ciclo semanal da conta B que está valendo agora, ou None."""
    semanas = semanas if semanas is not None else ler_semanas()
    ciclos = semanas.get("ciclos", [])
    return ciclos[-1] if ciclos else None


def abrir_ciclo(valor: float, inicio: str | None = None,
                linhas: list[dict] | None = None) -> dict:
    """Fecha a semana que estava aberta e começa outra do zero.

    Dizer "meu pai mandou 350" é o que fecha o período: tudo que você lançou até ali
    pertence à semana que está terminando, o gasto volta pra zero e o teto novo é o
    que ele mandou de fato.

    O que sobrou não evapora — vai pro acumulado, que é dinheiro do seu pai que ficou
    guardado. Se a semana estourou, o excesso sai do acumulado; ficando negativo, é
    sinal de que a diferença saiu do seu bolso.
    """
    inicio = inicio or hoje().isoformat()
    linhas = linhas or []
    semanas = ler_semanas()
    ciclos = semanas.setdefault("ciclos", [])
    semanas.setdefault("acumulado", 0.0)

    # Primeira vez: o que você lançou antes de existir teto não some, mas também não
    # entra no acumulado — não havia com o que comparar.
    if not ciclos:
        antes = [l for l in linhas if l["conta"] == "B" and l["tipo"] == "gasto"]
        if antes:
            semanas["periodo_inicial"] = {
                "ate": inicio,
                "gasto": round(sum(l["valor"] for l in antes), 2),
                "lancamentos": len(antes),
            }

    fechado = None
    if ciclos and not ciclos[-1].get("fechado_em"):
        anterior = ciclos[-1]
        gasto = gasto_no_ciclo(linhas, anterior)
        sobra = round(anterior["teto"] - gasto, 2)
        anterior.update({
            "fechado_em": inicio,
            "gasto_final": round(gasto, 2),
            "sobra": sobra,
        })
        semanas["acumulado"] = round(semanas["acumulado"] + sobra, 2)
        fechado = anterior

    # A fronteira é por ORDEM DE LANÇAMENTO, não por data. Data tem granularidade de
    # dia: gastar de manhã e o pai mandar à noite faria o mesmo gasto contar na semana
    # velha e na nova. O id resolve isso sem ambiguidade.
    corte = max((int(l["id"]) for l in linhas if str(l["id"]).isdigit()), default=0)

    ciclo = {
        "inicio": inicio,
        "desde": inicio,
        "desde_id": corte,
        "recebido": round(float(valor), 2),
        "teto": round(float(valor), 2),
    }
    ciclos.append(ciclo)
    gravar_semanas(semanas)
    ciclo["_fechado"] = fechado
    ciclo["_acumulado"] = semanas["acumulado"]
    return ciclo


def acumulado() -> float:
    """Sobra guardada das semanas já fechadas."""
    return round(float(ler_semanas().get("acumulado") or 0), 2)


def gasto_no_ciclo(linhas: list[dict], ciclo: dict) -> float:
    if not ciclo:
        return 0.0
    if ciclo.get("fechado_em"):
        return float(ciclo.get("gasto_final") or 0)  # semana fechada não muda mais
    if "desde_id" in ciclo:
        corte = ciclo["desde_id"]
        return sum(l["valor"] for l in linhas
                   if l["conta"] == "B" and l["tipo"] == "gasto"
                   and str(l["id"]).isdigit() and int(l["id"]) > corte)
    desde = ciclo.get("desde") or ciclo["inicio"]  # ciclos antigos, sem id
    return sum(l["valor"] for l in linhas
               if l["conta"] == "B" and l["tipo"] == "gasto" and l["data"] >= desde)


# ---------------------------------------------------------------- agregações


def mes_de(iso: str) -> str:
    return iso[:7]


def gastos_do_mes(linhas: list[dict], mes: str, conta: str = "A") -> float:
    return sum(
        l["valor"] for l in linhas
        if l["conta"] == conta and l["tipo"] == "gasto" and mes_de(l["data"]) == mes
    )


def receitas_do_mes(linhas: list[dict], mes: str, conta: str = "A") -> float:
    """Dinheiro que entrou fora do salário: amigo pagou, reembolso, freela."""
    return sum(
        l["valor"] for l in linhas
        if l["conta"] == conta and l["tipo"] == "receita" and mes_de(l["data"]) == mes
    )


def por_categoria(linhas: list[dict], mes: str, conta: str = "A") -> dict[str, float]:
    fora = {}
    for l in linhas:
        if l["conta"] != conta or l["tipo"] != "gasto" or mes_de(l["data"]) != mes:
            continue
        fora[l["categoria"]] = round(fora.get(l["categoria"], 0) + l["valor"], 2)
    return dict(sorted(fora.items(), key=lambda kv: -kv[1]))


def total_fatura(linhas: list[dict], fatura: str) -> float:
    return sum(l["valor"] for l in linhas if l.get("fatura") == fatura and l["tipo"] == "gasto")


def esperado_por_categoria(orcamento: dict) -> dict[str, float]:
    """Soma os itens do orçamento por categoria (Spotify + YouTube = Streaming).

    Um override definido por voz ("meu esperado de comer fora é 300") substitui a
    soma dos itens daquela categoria.
    """
    fora = {}
    for item in orcamento.get("A", {}).get("itens", []):
        c = item["categoria"]
        fora[c] = round(fora.get(c, 0) + float(item["esperado"]), 2)
    for c, v in orcamento.get("A", {}).get("overrides", {}).items():
        fora[c] = round(float(v), 2)
    return fora


def definir_esperado(orcamento: dict, categoria: str, valor: float) -> None:
    """Ajusta o esperado da categoria. Item único mexe direto; vários viram override."""
    itens = [i for i in orcamento.get("A", {}).get("itens", []) if i["categoria"] == categoria]
    if len(itens) == 1:
        itens[0]["esperado"] = round(float(valor), 2)
        orcamento["A"].get("overrides", {}).pop(categoria, None)
    else:
        orcamento.setdefault("A", {}).setdefault("overrides", {})[categoria] = round(float(valor), 2)


def brl(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"-R$ {s}" if v < 0 else f"R$ {s}"


def semana_passou(ciclo: dict, dias: int = 8) -> bool:
    """Faz mais de `dias` que o pai não manda — vale perguntar uma vez."""
    if not ciclo:
        return True
    return (hoje() - date.fromisoformat(ciclo["inicio"])).days >= dias
