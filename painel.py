#!/usr/bin/env python3
"""Gera o painel.html: um arquivo só, sem dependência externa, claro e escuro.

Conta A = azul, conta B = laranja, sempre. A cor segue a carteira, nunca a posição
na lista — se você filtrar, o que sobra não muda de cor.
"""

import html
import os
from datetime import date, timedelta

import dados as D

SAIDA = os.path.join(D.BASE, "painel.html")

MESES = ["jan", "fev", "mar", "abr", "mai", "jun",
         "jul", "ago", "set", "out", "nov", "dez"]


def rotulo_mes(m: str) -> str:
    return f"{MESES[int(m[5:7]) - 1]}/{m[2:4]}"


def e(t) -> str:
    return html.escape(str(t), quote=True)


CSS = """
:root{
  color-scheme:light;
  --paper:#f7f7f5; --card:#ffffff; --ink:#15171a; --ink-2:#54585f; --muted:#8a8e96;
  --line:#e6e6e1; --line-2:#d5d5cf; --track:#eeeeea;
  --conta-a:#2a78d6; --conta-b:#eb6834;
  --cut:#e34948; --cut-soft:#fbeceb; --ok:#0d8a4f;
  --shadow:0 1px 2px rgba(20,22,26,.04),0 8px 24px rgba(20,22,26,.05);
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --paper:#0e0f11; --card:#191b1e; --ink:#f3f3f0; --ink-2:#b6bac1; --muted:#7c8088;
    --line:#2a2c30; --line-2:#34373c; --track:#232629;
    --conta-a:#3987e5; --conta-b:#d95926;
    --cut:#e66767; --cut-soft:#341f1f; --ok:#3fbf7f;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --paper:#0e0f11; --card:#191b1e; --ink:#f3f3f0; --ink-2:#b6bac1; --muted:#7c8088;
  --line:#2a2c30; --line-2:#34373c; --track:#232629;
  --conta-a:#3987e5; --conta-b:#d95926;
  --cut:#e66767; --cut-soft:#341f1f; --ok:#3fbf7f;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25);
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;line-height:1.5;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:36px 20px 80px}
.num{font-variant-numeric:tabular-nums}
header{margin-bottom:28px}
.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:600;
  color:var(--muted);margin:0 0 8px}
h1{font-size:clamp(24px,4vw,34px);line-height:1.1;margin:0 0 6px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--ink-2);font-size:14px;margin:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:24px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:15px 17px;
  box-shadow:var(--shadow);border-top:3px solid var(--line-2)}
.kpi.a{border-top-color:var(--conta-a)} .kpi.b{border-top-color:var(--conta-b)}
.kpi .v{font-size:22px;font-weight:700;letter-spacing:-.01em}
.kpi .v.cut{color:var(--cut)} .kpi .v.ok{color:var(--ok)}
.kpi .l{font-size:12.5px;color:var(--ink-2);margin-top:2px}
section{background:var(--card);border:1px solid var(--line);border-radius:16px;
  padding:22px 22px 24px;margin-top:20px;box-shadow:var(--shadow)}
h2{font-size:18px;font-weight:700;margin:0 0 3px;letter-spacing:-.01em;display:flex;
  align-items:center;gap:9px}
.dot{width:10px;height:10px;border-radius:3px;flex:none}
.dot.a{background:var(--conta-a)} .dot.b{background:var(--conta-b)}
.lead{color:var(--ink-2);font-size:13px;margin:0 0 18px}
.bars{display:flex;flex-direction:column;gap:11px}
.row{display:grid;grid-template-columns:minmax(80px,148px) 1fr 116px;align-items:center;gap:12px}
.lbl{font-size:13px;color:var(--ink-2);text-align:right;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.track{background:var(--track);border-radius:5px;height:20px;position:relative;overflow:hidden}
.fill{height:100%;border-radius:5px 4px 4px 5px;min-width:3px;background:var(--conta-a);
  transition:width .2s}
.fill.b{background:var(--conta-b)}
.fill.cut{background:var(--cut)}
.mark{position:absolute;top:0;bottom:0;width:2px;background:var(--ink);opacity:.45}
.val{font-size:12.5px;text-align:right;color:var(--muted);line-height:1.25;white-space:nowrap}
.val b{color:var(--ink);font-weight:700;font-size:13.5px;display:block}
.tag{display:inline-block;font-size:9.5px;font-weight:700;text-transform:uppercase;
  letter-spacing:.05em;padding:1px 5px;border-radius:5px;
  background:var(--cut-soft);color:var(--cut)}
@media (max-width:560px){
  .row{grid-template-columns:1fr 96px;gap:8px}
  .lbl{grid-column:1/-1;text-align:left;font-size:12px;margin-bottom:-6px}
}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--ink-2);margin:0 0 16px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:560px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  font-weight:600;padding:0 10px 8px;border-bottom:1px solid var(--line-2)}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
td.r{text-align:right;font-weight:600}
.pill{display:inline-block;width:20px;text-align:center;font-size:10.5px;font-weight:700;
  border-radius:5px;padding:1px 0;color:#fff;background:var(--conta-a)}
.pill.b{background:var(--conta-b)}
.mic{color:var(--muted);font-size:11.5px;font-style:italic;display:block;margin-top:2px;
  max-width:44ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vazio{color:var(--muted);font-size:13.5px;margin:0}
footer{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
[data-tip]{cursor:default}
[data-tip]:hover::after{content:attr(data-tip);position:absolute;left:8px;top:-30px;z-index:9;
  background:var(--ink);color:var(--paper);font-size:11.5px;padding:4px 8px;border-radius:6px;
  white-space:nowrap;pointer-events:none}
"""


def barra(rotulo, valor, teto, maximo, classe="", tip=""):
    """Uma linha: rótulo, barra, número. A marca vertical é o esperado."""
    largura = min(100.0, (valor / maximo * 100) if maximo else 0)
    estourou = teto and valor > teto
    cls = "fill cut" if estourou else f"fill {classe}"
    marca = ""
    if teto and maximo and teto < maximo:
        marca = f'<span class="mark" style="left:{min(100.0, teto / maximo * 100):.1f}%"></span>'
    if estourou:
        rodape = '<span class="tag">estourou</span>'
    elif teto:
        rodape = f"de {e(D.brl(teto))}"
    else:
        rodape = ""
    return (
        f'<div class="row">'
        f'<div class="lbl">{e(rotulo)}</div>'
        f'<div class="track" data-tip="{e(tip or rotulo)}">'
        f'<div class="{cls}" style="width:{largura:.1f}%"></div>{marca}</div>'
        f'<div class="val num"><b>{D.brl(valor)}</b>{rodape}</div>'
        f'</div>'
    )


def bloco_a(linhas, orcamento, mes):
    renda = float(orcamento.get("A", {}).get("renda") or 0)
    esperado = D.esperado_por_categoria(orcamento)
    real = D.por_categoria(linhas, mes, "A")
    cats = sorted(set(esperado) | set(real), key=lambda c: -(real.get(c, 0) or esperado.get(c, 0)))
    maximo = max([*(real.get(c, 0) for c in cats), *esperado.values(), 1])

    corpo = "".join(
        barra(c, real.get(c, 0), esperado.get(c, 0), maximo, "",
              f"{c}: gasto {D.brl(real.get(c, 0))}"
              + (f", esperado {D.brl(esperado[c])}" if c in esperado else ""))
        for c in cats
    ) or '<p class="vazio">Nenhum gasto seu registrado nesse mês ainda.</p>'

    return (
        f'<section><h2><span class="dot a"></span>Conta A — seu salário</h2>'
        f'<p class="lead">Teto de {D.brl(renda)} por mês. A marca escura em cada barra é o '
        f'valor esperado que você definiu.</p>'
        f'<div class="bars">{corpo}</div></section>'
    )


def bloco_b(linhas, orcamento, mes):
    ciclo = D.periodo_aberto(linhas)
    real = D.por_categoria(linhas, mes, "B")
    gasto = D.gasto_no_ciclo(linhas, ciclo)
    ini = ciclo["inicio"]
    cabecalho = (f'Acumulando desde {ini[8:10]}/{ini[5:7]}: <b>{D.brl(gasto)}</b>. '
                 'Diga <b>&ldquo;meu pai mandou 350&rdquo;</b> no Telegram pra fechar o '
                 'período e ver a sobra.')
    topo = ""

    acum = D.acumulado()
    if acum:
        cabecalho += (f" Guardado das semanas fechadas: <b>{D.brl(acum)}</b>."
                      if acum > 0 else
                      f" Guardado negativo (<b>{D.brl(acum)}</b>): essa diferença saiu do seu bolso.")

    fechados = [c for c in D.ler_semanas().get("ciclos", []) if c.get("fechado_em")][-6:]
    historico = ""
    if fechados:
        itens = "".join(
            f'<div class="row"><div class="lbl">{e(c["inicio"][8:10])}/{e(c["inicio"][5:7])}'
            f' → {e(c["fechado_em"][8:10])}/{e(c["fechado_em"][5:7])}</div>'
            f'<div class="track" data-tip="gastou {D.brl(c["gasto"])} · '
            f'recebeu {D.brl(c["recebido"])}">'
            f'<div class="fill {"cut" if c["sobra"] < 0 else "b"}" '
            f'style="width:{min(100.0, c["gasto"] / max(c["recebido"], 1) * 100):.1f}%"></div></div>'
            f'<div class="val num"><b>{D.brl(c["gasto"])}</b>'
            f'{"sobrou " + D.brl(c["sobra"]) if c["sobra"] >= 0 else "faltou"}</div></div>'
            for c in fechados
        )
        historico = (f'<p class="lead" style="margin:22px 0 10px">Períodos fechados</p>'
                     f'<div class="bars">{itens}</div>')

    real = D.categorias_do_periodo(linhas, ciclo)
    maximo = max([*real.values(), 1])
    detalhe = "".join(
        barra(c, v, 0, maximo, "b", f"{c}: {D.brl(v)} no período") for c, v in real.items()
    )
    return (
        f'<section><h2><span class="dot b"></span>Conta B — ajuda do pai</h2>'
        f'<p class="lead">{cabecalho} Nada daqui entra no seu orçamento mensal.</p>'
        f'<div class="bars">{topo}{detalhe}</div>{historico}</section>'
    )


def bloco_fatura(linhas):
    fatura = D.fatura_aberta()
    total = D.total_fatura(linhas, fatura)
    fecha, dias_f = D.fecha_em()
    vence, dias_v = D.vence_em()

    porfatura = {}
    for l in linhas:
        if l.get("fatura") and l["tipo"] == "gasto":
            porfatura[l["fatura"]] = round(porfatura.get(l["fatura"], 0) + l["valor"], 2)
    porfatura.setdefault(fatura, 0.0)  # a aberta sempre aparece, mesmo zerada

    recentes = sorted(porfatura.items())[-8:]
    maximo = max([v for _, v in recentes] + [1])
    corpo = "".join(
        barra(("▶ " if f == fatura else "") + f"vence {f[8:10]}/{f[5:7]}", v, 0, maximo, "",
              f"fatura de {f}: {D.brl(v)}" + (" (aberta)" if f == fatura else ""))
        for f, v in recentes
    ) or '<p class="vazio">Nenhuma compra no crédito registrada ainda.</p>'

    return (
        f'<section><h2>💳 Cartão</h2>'
        f'<p class="lead">Fecha dia {D.FECHAMENTO}, vence dia {D.VENCIMENTO}. Compra feita a partir '
        f'do dia {D.FECHAMENTO + 1} só é paga no mês seguinte — por isso a data em que você gasta '
        f'não é a data em que o dinheiro sai.</p>'
        f'<div class="bars">{corpo}</div>'
        f'<p class="lead" style="margin:16px 0 0">Fatura aberta: <b>{D.brl(total)}</b> · '
        f'fecha em {dias_f} dia(s) ({fecha.strftime("%d/%m")}) · '
        f'vence em {dias_v} dia(s) ({vence.strftime("%d/%m")}).</p></section>'
    )


def bloco_historico(linhas):
    meses = sorted({D.mes_de(l) for l in linhas if l["tipo"] == "gasto"})[-12:]
    if len(meses) < 2:
        return ""
    dados = [(m, D.gastos_do_mes(linhas, m, "A"), D.gastos_do_mes(linhas, m, "B")) for m in meses]
    maximo = max([max(a, b) for _, a, b in dados] + [1])

    linhas_html = []
    for m, a, b in dados:
        linhas_html.append(
            f'<div class="row"><div class="lbl">{e(rotulo_mes(m))}</div>'
            f'<div class="track" data-tip="A {D.brl(a)} · B {D.brl(b)}">'
            f'<div class="fill" style="width:{a / maximo * 100:.1f}%"></div></div>'
            f'<div class="val num"><b>{D.brl(a)}</b></div></div>'
            f'<div class="row" style="margin-top:-7px"><div class="lbl"></div>'
            f'<div class="track" data-tip="A {D.brl(a)} · B {D.brl(b)}">'
            f'<div class="fill b" style="width:{b / maximo * 100:.1f}%"></div></div>'
            f'<div class="val num">{D.brl(b)}</div></div>'
        )
    return (
        '<section><h2>📈 Mês a mês</h2>'
        '<p class="lead">Gasto por carteira, por mês de competência (data da compra).</p>'
        '<p class="legend"><span><i class="dot a"></i>Conta A — seu salário</span>'
        '<span><i class="dot b"></i>Conta B — ajuda do pai</span></p>'
        f'<div class="bars">{"".join(linhas_html)}</div></section>'
    )


def bloco_lancamentos(linhas, n=40):
    recentes = sorted(linhas, key=lambda l: (l["data"], int(l["id"])), reverse=True)[:n]
    if not recentes:
        return ('<section><h2>🧾 Lançamentos</h2>'
                '<p class="vazio">Nada ainda. Manda um áudio no grupo do Telegram.</p></section>')
    tr = []
    for l in recentes:
        sinal = "＋" if l["tipo"] == "receita" else ""
        mic = f'<span class="mic">🎙 {e(l["transcricao"][:90])}</span>' if l["transcricao"] else ""
        tr.append(
            f'<tr><td class="num">{e(l["data"][8:10])}/{e(l["data"][5:7])}</td>'
            f'<td><span class="pill{" b" if l["conta"] == "B" else ""}">{e(l["conta"])}</span></td>'
            f'<td>{e(l["categoria"])}</td>'
            f'<td>{e(l["descricao"])}{mic}</td>'
            f'<td class="r num">{sinal}{D.brl(l["valor"])}</td></tr>'
        )
    return (
        '<section><h2>🧾 Últimos lançamentos</h2>'
        '<p class="lead">A transcrição fica junto: se o número saiu errado, dá pra ver na hora se '
        'foi erro de ouvido ou de interpretação.</p>'
        '<div class="scroll"><table><thead><tr><th>Data</th><th>Conta</th><th>Categoria</th>'
        f'<th>Descrição</th><th style="text-align:right">Valor</th></tr></thead>'
        f'<tbody>{"".join(tr)}</tbody></table></div></section>'
    )


def kpis(linhas, orcamento, mes):
    renda = float(orcamento.get("A", {}).get("renda") or 0)
    gasto_a = D.gastos_do_mes(linhas, mes, "A")
    sobra = renda + D.receitas_do_mes(linhas, mes, "A") - gasto_a
    total_fat = D.total_fatura(linhas, D.fatura_aberta())
    _, dias_f = D.fecha_em()

    gasto_b = D.gasto_no_ciclo(linhas, D.periodo_aberto(linhas))
    kpi_b = (f'<div class="kpi b"><div class="v num">{D.brl(gasto_b)}</div>'
             f'<div class="l">Acumulado da conta do pai</div></div>')

    return (
        '<div class="kpis">'
        f'<div class="kpi a"><div class="v num {"ok" if sobra >= 0 else "cut"}">{D.brl(sobra)}</div>'
        f'<div class="l">Sobra do mês</div></div>'
        f'<div class="kpi a"><div class="v num">{D.brl(gasto_a)}</div>'
        f'<div class="l">Gasto seu em {rotulo_mes(mes)}</div></div>'
        f'{kpi_b}'
        f'<div class="kpi"><div class="v num">{D.brl(total_fat)}</div>'
        f'<div class="l">Fatura aberta · fecha em {dias_f}d</div></div>'
        '</div>'
    )


def gerar(linhas=None, orcamento=None, saida=SAIDA) -> str:
    linhas = D.ler_lancamentos() if linhas is None else linhas
    orcamento = D.ler_orcamento() if orcamento is None else orcamento
    mes = D.mes_aberto_a()

    corpo = (
        '<header><p class="eyebrow">Controle financeiro</p>'
        f'<h1>Painel do Igor</h1>'
        f'<p class="sub">Duas carteiras separadas: o seu salário e a ajuda do seu pai. '
        f'Elas nunca se somam.</p>'
        f'{kpis(linhas, orcamento, mes)}</header>'
        f'{bloco_a(linhas, orcamento, mes)}'
        f'{bloco_b(linhas, orcamento, mes)}'
        f'{bloco_fatura(linhas)}'
        f'{bloco_historico(linhas)}'
        f'{bloco_lancamentos(linhas)}'
        f'<footer>Gerado em {D.agora().strftime("%d/%m/%Y às %H:%M")} · '
        f'{len(linhas)} lançamento(s)</footer>'
    )

    doc = (
        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Painel do Igor — Controle Financeiro</title>'
        f'<style>{CSS}\n.legend i{{width:10px;height:10px;border-radius:3px;display:inline-block}}'
        '\n.legend i.a{background:var(--conta-a)}\n.legend i.b{background:var(--conta-b)}</style>'
        f'</head><body><div class="wrap">{corpo}</div></body></html>'
    )
    with open(saida, "w", encoding="utf-8") as f:
        f.write(doc)
    return saida


if __name__ == "__main__":
    print(gerar())
