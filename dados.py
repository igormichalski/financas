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
# Dourados/MS é UTC-4, uma hora atrás de Brasília. Usar o fuso errado desloca a data
# de qualquer lançamento feito perto da meia-noite.
TZ = ZoneInfo(os.environ.get("FUSO", "America/Campo_Grande"))

LANCAMENTOS = os.path.join(BASE, "lancamentos.csv")
ORCAMENTO = os.path.join(BASE, "orcamento.json")
RECORRENTES = os.path.join(BASE, "recorrentes.json")
SEMANAS = os.path.join(BASE, "semanas.json")
PENDENCIAS = os.path.join(BASE, "pendencias.json")
STATE = os.path.join(BASE, "state.json")
REVISAR = os.path.join(BASE, "revisar.csv")
FILA = os.path.join(BASE, "fila.json")
# Onde o Worker do Cloudflare larga cada mensagem crua. Mora aqui, e não solto no
# sync.py, pra que o sandbox dos testes redirecione junto com o resto.
INBOX = os.path.join(BASE, "inbox")

# Bots do Telegram só baixam arquivo até 20 MB; o Gemini aceita ~20 MB inline.
LIMITE_AUDIO = 18 * 1024 * 1024
# Depois disso o item sai da fila pra não travar tudo pra sempre.
MAX_TENTATIVAS = 3

# Cartão Nubank: fecha dia 15, vence dia 22.
FECHAMENTO = 15
VENCIMENTO = 22

# Dia em que o ciclo mensal da conta A vira sozinho. NÃO é o fechamento do cartão:
# o teto do salário e a fatura são ciclos independentes.
DIA_VIRADA = 10

CAMPOS = [
    "id", "ts", "data", "tipo", "valor", "conta", "categoria", "descricao",
    "pagamento", "fatura", "origem", "confianca", "conta_origem",
    "transcricao", "msg_id", "mes_ref"
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
        if not l.get("mes_ref"):
            # Tem que ser a MESMA regra do novo_lancamento. Usar l["data"][:7] aqui
            # faz linha editada à mão cair num mês e linha nova do mesmo dia cair noutro.
            l["mes_ref"] = ciclo_de(l["data"])
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
    
    mes_ref = campos.get("mes_ref")
    if not mes_ref:
        mes_ref = ciclo_de(d)

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
        "mes_ref": mes_ref,
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
    """O período da conta B que está acumulando gasto agora, ou None.

    Período aberto NÃO tem teto: você gasta durante os dias e só depois seu pai
    manda o dinheiro. É o valor que ele manda que fecha o período e revela a sobra.
    """
    semanas = semanas if semanas is not None else ler_semanas()
    ciclos = [c for c in semanas.get("ciclos", []) if not c.get("fechado_em")]
    return ciclos[-1] if ciclos else None


def periodo_aberto(linhas: list[dict] | None = None, semanas=None) -> dict:
    """O período que está acumulando agora. Sempre existe, mesmo antes do primeiro
    fechamento — aí ele é implícito, começando no seu gasto de conta B mais antigo.
    Não grava nada: período só vira registro quando fecha."""
    aberto = ciclo_aberto(semanas)
    if aberto:
        return aberto
    gastos_b = [l for l in (linhas or []) if l["conta"] == "B" and l["tipo"] == "gasto"]
    return {"inicio": min((l["data"] for l in gastos_b), default=hoje().isoformat()),
            "desde_id": 0}


def fechar_periodo(valor: float, quando: str | None = None,
                   linhas: list[dict] | None = None) -> dict:
    """Dizer "meu pai mandou X" fecha o período com o que você JÁ gastou.

    A ordem real é essa: você gasta durante os dias, e depois ele manda o dinheiro
    cobrindo aquilo. Então o valor anunciado se aplica ao gasto que já aconteceu —
    não é teto pra semana que vem. `sobra = recebido − gasto acumulado`.

    Fechado, o período vai pro histórico e um novo começa vazio a partir dali. A
    fronteira é por ordem de lançamento, não por data: gastar de manhã e ele mandar
    à noite não pode fazer o mesmo gasto contar em dois períodos.
    """
    quando = quando or hoje().isoformat()
    linhas = linhas or []
    semanas = ler_semanas()
    semanas.setdefault("ciclos", [])
    semanas.setdefault("acumulado", 0.0)

    aberto = ciclo_aberto(semanas)
    if aberto is None:  # primeiro fechamento: o período implícito vira registro
        aberto = periodo_aberto(linhas, semanas)
        semanas["ciclos"].append(aberto)

    gasto = gasto_no_ciclo(linhas, aberto)
    sobra = round(float(valor) - gasto, 2)

    aberto.update({
        "fechado_em": quando,
        "recebido": round(float(valor), 2),
        "gasto": round(gasto, 2),
        "sobra": sobra,
    })
    semanas["acumulado"] = round(semanas.get("acumulado", 0.0) + sobra, 2)

    corte = max((int(l["id"]) for l in linhas if str(l["id"]).isdigit()), default=0)
    semanas["ciclos"].append({"inicio": quando, "desde_id": corte})
    gravar_semanas(semanas)

    return {"fechado": aberto, "acumulado": semanas["acumulado"]}


def acumulado() -> float:
    """Sobra guardada dos períodos já fechados."""
    return round(float(ler_semanas().get("acumulado") or 0), 2)


def categorias_do_periodo(linhas: list[dict], ciclo: dict) -> dict[str, float]:
    """Gasto por categoria dentro do período aberto — não do mês."""
    if not ciclo or ciclo.get("fechado_em"):
        return {}
    corte = ciclo.get("desde_id", 0)
    fora = {}
    for l in linhas:
        if (l["conta"] == "B" and l["tipo"] == "gasto"
                and str(l["id"]).isdigit() and int(l["id"]) > corte):
            fora[l["categoria"]] = round(fora.get(l["categoria"], 0) + l["valor"], 2)
    return dict(sorted(fora.items(), key=lambda kv: -kv[1]))


def gasto_no_ciclo(linhas: list[dict], ciclo: dict) -> float:
    if not ciclo:
        return 0.0
    if ciclo.get("fechado_em"):
        return float(ciclo.get("gasto") or ciclo.get("gasto_final") or 0)
    corte = ciclo.get("desde_id", 0)
    return sum(l["valor"] for l in linhas
               if l["conta"] == "B" and l["tipo"] == "gasto"
               and str(l["id"]).isdigit() and int(l["id"]) > corte)


# ---------------------------------------------------------------- agregações


def ciclo_de(d_str: str) -> str:
    """Em qual ciclo mensal da conta A a data cai. O mês vira sozinho no dia 10.

    Gasto de 05/ago ainda pertence ao ciclo de julho; gasto de 10/ago abre agosto.
    O rótulo é "AAAA-MM" do mês em que o ciclo COMEÇOU, então o ciclo que vai de
    10/jul a 09/ago se chama "2026-07".

    Atenção: isso não é o ciclo da FATURA (que fecha dia 15, veja fatura_de). São
    duas coisas diferentes de propósito — uma é o teto do salário, a outra é quando
    o Nubank cobra.
    """
    try:
        d = date.fromisoformat(d_str[:10])
    except ValueError:
        return d_str[:7]
    if d.day >= DIA_VIRADA:
        return f"{d.year:04d}-{d.month:02d}"
    if d.month == 1:
        return f"{d.year - 1:04d}-12"
    return f"{d.year:04d}-{d.month - 1:02d}"


def mes_aberto_a() -> str:
    return ciclo_de(hoje().isoformat())


def proximo_mes(mes: str) -> str:
    """"2026-12" → "2027-01". Usado pra achar o ciclo seguinte sem repetir aritmética."""
    ano, m = int(mes[:4]), int(mes[5:7])
    return f"{ano + 1:04d}-01" if m == 12 else f"{ano:04d}-{m + 1:02d}"


def mes_anterior(mes: str) -> str:
    ano, m = int(mes[:4]), int(mes[5:7])
    return f"{ano - 1:04d}-12" if m == 1 else f"{ano:04d}-{m - 1:02d}"


def mes_de(l: dict | str) -> str:
    if isinstance(l, dict):
        return l.get("mes_ref") or ciclo_de(l["data"])
    return l[:7]


def gastos_do_mes(linhas: list[dict], mes: str, conta: str = "A") -> float:
    return sum(
        l["valor"] for l in linhas
        if l["conta"] == conta and l["tipo"] == "gasto" and mes_de(l) == mes
    )


def receitas_do_mes(linhas: list[dict], mes: str, conta: str = "A") -> float:
    """Dinheiro que entrou fora do salário: amigo pagou, reembolso, freela."""
    return sum(
        l["valor"] for l in linhas
        if l["conta"] == conta and l["tipo"] == "receita" and mes_de(l) == mes
    )


def por_categoria(linhas: list[dict], mes: str, conta: str = "A") -> dict[str, float]:
    fora = {}
    for l in linhas:
        if l["conta"] != conta or l["tipo"] != "gasto" or mes_de(l) != mes:
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


def num(v: float) -> str:
    """Só o número, no formato BR — pra alinhar em coluna."""
    s = f"{abs(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"-{s}" if v < 0 else s


def brl(v: float) -> str:
    return ("-R$ " if v < 0 else "R$ ") + num(v).lstrip("-")


# Nome curto pra caber na tabela do Telegram sem quebrar linha.
CURTO = {
    "Restaurantes/Lanches": "Comer fora",
    "Mercado/Supermercado": "Mercado",
    "Marmita (Fitfood)": "Marmita",
    "Transporte/App": "Transporte",
    "Saúde/Academia": "Saúde",
    "Serviços online/Tech": "Tech",
    "Educação/Cursos": "Educação",
    "Viagem/Passagens": "Viagem",
    "Pix p/ pessoas": "Pix",
    "Combustível": "Combust.",
}


def curto(categoria: str) -> str:
    return CURTO.get(categoria, categoria)


def semana_passou(ciclo: dict, dias: int = 8) -> bool:
    """Faz mais de `dias` que o pai não manda — vale perguntar uma vez."""
    if not ciclo:
        return True
    return (hoje() - date.fromisoformat(ciclo["inicio"])).days >= dias
