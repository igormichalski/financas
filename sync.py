#!/usr/bin/env python3
"""Orquestrador: lê o Telegram, despacha as intenções, atualiza o ledger e responde.

Roda no GitHub Actions de 30 em 30 min. Nada fica ligado no PC do Igor.
"""

import os
import sys
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

# Espaçamento entre mensagens da fila, pra não estourar o limite por minuto do Gemini.
PAUSA_ENTRE_ITENS = float(os.environ.get("PAUSA_ENTRE_ITENS", "4"))

# Cadência adaptativa. O cron acorda de 30 em 30 min; se ao acordar encontrar
# movimento (mensagem nova ou fila pendente), o run FICA VIVO checando de 3 em 3 min
# em vez de dormir meia hora. Enquanto você está lançando, a resposta é rápida;
# parou de mexer, ele encerra e volta ao ritmo lento.
CICLO_RAPIDO = int(os.environ.get("CICLO_RAPIDO", "180"))      # 3 min entre checagens
JANELA_ATIVA = int(os.environ.get("JANELA_ATIVA", "900"))      # 15 min quieto → encerra
TEMPO_MAX = int(os.environ.get("TEMPO_MAX", "1500"))           # 25 min de teto por run
MAX_SEM_PROGRESSO = int(os.environ.get("MAX_SEM_PROGRESSO", "2"))

import dados as D
import erros
import extrator
import painel
from telegram import Telegram, extrair_midia

AFIRMA = {"sim", "s", "isso", "aham", "pode", "confirma", "confirmo", "positivo",
          "exato", "certo", "correto", "beleza", "blz", "ok", "pagou", "paguei", "é"}
NEGA = {"não", "nao", "n", "negativo", "cancela", "deixa", "esquece", "errado"}


def _limpa(t: str) -> str:
    return "".join(c for c in (t or "").lower().strip() if c.isalnum() or c.isspace()).strip()


def afirmativo(texto: str) -> bool:
    palavras = set(_limpa(texto).split())
    return bool(palavras & AFIRMA) and not (palavras & NEGA)


def negativo(texto: str) -> bool:
    return bool(set(_limpa(texto).split()) & NEGA)


# ---------------------------------------------------------------- contexto


def contexto_semana(linhas):
    ciclo = D.ciclo_aberto()
    if not ciclo:
        return None
    gasto = D.gasto_no_ciclo(linhas, ciclo)
    return {"ciclo": ciclo, "gasto": round(gasto, 2), "resta": round(ciclo["teto"] - gasto, 2)}


def recentes(linhas, n=20):
    return sorted(linhas, key=lambda l: (l["data"], int(l["id"])))[-n:]


# ---------------------------------------------------------------- consultas


def responder_consulta(linhas, c: dict) -> str:
    """Calculado aqui, não pelo modelo — número inventado é pior que nenhum número."""
    periodo = (c or {}).get("periodo") or "mes"
    categoria = (c or {}).get("categoria") or ""
    conta = (c or {}).get("conta") or "ambas"
    hoje = D.hoje()

    if periodo == "hoje":
        alvo, rotulo = lambda l: l["data"] == hoje.isoformat(), "hoje"
    elif periodo == "semana":
        ciclo = D.ciclo_aberto()
        inicio = ciclo["inicio"] if ciclo else (hoje - timedelta(days=7)).isoformat()
        alvo, rotulo = lambda l: l["data"] >= inicio, f"desde {inicio[8:10]}/{inicio[5:7]}"
    elif periodo == "tudo":
        alvo, rotulo = lambda l: True, "no total"
    else:
        mes = hoje.isoformat()[:7]
        alvo, rotulo = lambda l: l["data"][:7] == mes, "esse mês"

    sel = [l for l in linhas if l["tipo"] == "gasto" and alvo(l)]
    if conta in ("A", "B"):
        sel = [l for l in sel if l["conta"] == conta]
    if categoria:
        sel = [l for l in sel if l["categoria"] == categoria]

    total = sum(l["valor"] for l in sel)
    alvo_txt = categoria or ("conta " + conta if conta in ("A", "B") else "tudo")
    linha = f"📊 <b>{alvo_txt}</b> {rotulo}: {D.brl(total)} em {len(sel)} lançamento(s)"

    if categoria:
        esperado = D.esperado_por_categoria(D.ler_orcamento()).get(categoria)
        if esperado and periodo == "mes":
            resta = esperado - total
            linha += (f"\nEsperado {D.brl(esperado)} · "
                      + (f"restam {D.brl(resta)}" if resta >= 0 else f"⚠️ estourou {D.brl(-resta)}"))
    if not categoria and sel:
        top = {}
        for l in sel:
            top[l["categoria"]] = top.get(l["categoria"], 0) + l["valor"]
        maiores = sorted(top.items(), key=lambda kv: -kv[1])[:5]
        linha += "\n" + "\n".join(f"  · {k}: {D.brl(v)}" for k, v in maiores)
    return linha


def responder_fatura(linhas) -> str:
    fatura = D.fatura_aberta()
    total = D.total_fatura(linhas, fatura)
    fecha, dias_f = D.fecha_em()
    vence, dias_v = D.vence_em()
    return (f"💳 <b>Fatura aberta</b> (vence {fatura[8:10]}/{fatura[5:7]}): {D.brl(total)}\n"
            f"Fecha em {dias_f} dia(s) — {fecha.strftime('%d/%m')}. "
            f"Próximo vencimento em {dias_v} dia(s).\n"
            f"<i>Compra depois do dia {D.FECHAMENTO} só é paga no mês seguinte.</i>")


# ---------------------------------------------------------------- resumo


MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
         "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]

# Motivo curto pra ir junto do "X mensagens na fila", em português de gente.
MOTIVO = {
    "gemini-rpm": "limite por minuto do Gemini",
    "gemini-cota-dia": "cota diária do Gemini acabou",
    "gemini-instavel": "Gemini fora do ar",
    "gemini-json": "Gemini devolveu resposta quebrada",
    "gemini-vazio": "Gemini devolveu vazio",
    "rede-Gemini": "sem conexão com o Gemini",
    "rede-Telegram": "sem conexão com o Telegram",
    "tg-flood": "Telegram limitou o envio",
    "tg-instavel": "Telegram fora do ar",
}

TRACO = "━" * 18


def _bloco(linhas: list[str]) -> str:
    """Bloco monoespaçado. Número alinhado em coluna é o que faz ler de relance."""
    corpo = "\n".join(linhas)
    return f"<pre>{corpo}</pre>"


def _barra(gasto: float, teto: float, largura: int = 6) -> str:
    if teto <= 0:
        return " " * largura
    cheio = min(largura, round(gasto / teto * largura))
    return "▓" * cheio + "░" * (largura - cheio)


def _linha_meta(rotulo: str, gasto: float, teto: float) -> str:
    """Largura pensada pra caber na tela do celular sem rolagem lateral."""
    marca = " ⚠" if gasto > teto else ""
    return (f"{rotulo[:10]:<10} {_barra(gasto, teto)} "
            f"{D.num(gasto):>8}/{D.num(teto):>8}{marca}")


def montar_resumo(linhas, orcamento, novos=()) -> str:
    partes = []

    if novos:
        partes.append(f"✅ <b>{len(novos)} lançamento{'s' if len(novos) > 1 else ''}</b>")
        tabela = []
        for l in novos:
            sinal = "+" if l["tipo"] == "receita" else " "
            tabela.append(f"{l['conta']} {sinal}{D.num(l['valor']):>9}  "
                          f"{l['descricao'][:15]:<15} {D.curto(l['categoria'])[:11]}")
        partes.append(_bloco(tabela))
        for l in novos:
            if l["transcricao"]:
                partes.append(f"🎙 <i>{l['transcricao'][:110]}</i>")
        partes.append(TRACO)

    mes = D.hoje().isoformat()[:7]
    renda = float(orcamento.get("A", {}).get("renda") or 0)
    gasto_a = D.gastos_do_mes(linhas, mes, "A")
    extra = D.receitas_do_mes(linhas, mes, "A")
    disponivel = renda + extra

    partes.append(f"🅰️ <b>SEU DINHEIRO</b> · {MESES[int(mes[5:7]) - 1]}")
    topo = [f"{'gasto':<7}{D.num(gasto_a):>10}",
            f"{'sobra':<7}{D.num(disponivel - gasto_a):>10}  de {D.num(disponivel)}"]
    if extra:
        topo.append(f"{'':7}{'':>10}  salário + {D.num(extra)}")
    partes.append(_bloco(topo))

    # Só o que teve movimento ou estourou. Categoria zerada é ruído — está no painel.
    esperado = D.esperado_por_categoria(orcamento)
    real = D.por_categoria(linhas, mes, "A")
    metas = [_linha_meta(D.curto(c), real.get(c, 0), e)
             for c, e in sorted(esperado.items(), key=lambda kv: -kv[1])
             if real.get(c, 0) > 0]
    sobrando = [c for c in esperado if real.get(c, 0) == 0]
    if metas:
        partes.append(_bloco(metas))
    if sobrando:
        partes.append(f"<i>sem gasto ainda: {', '.join(D.curto(c) for c in sobrando)}</i>")

    _, dias = D.fecha_em()
    partes.append(f"💳 fatura aberta <b>{D.brl(D.total_fatura(linhas, D.fatura_aberta()))}</b> "
                  f"· fecha em {dias}d")

    partes.append("")
    sem = contexto_semana(linhas)
    if sem:
        ini = sem["ciclo"]["inicio"]
        partes.append(f"🅱️ <b>CONTA DO PAI</b> · desde {ini[8:10]}/{ini[5:7]}")
        bloco = [_linha_meta("semana", sem["gasto"], sem["ciclo"]["teto"]),
                 f"{'restam' if sem['resta'] >= 0 else 'estourou':<10} {'':6} "
                 f"{D.num(abs(sem['resta'])):>8}"]
        partes.append(_bloco(bloco))
        catb = D.por_categoria(linhas, mes, "B")
        if catb:
            partes.append(_bloco([f"{D.curto(k)[:10]:<10} {D.num(v):>8}"
                                  for k, v in list(catb.items())[:5]]))
    else:
        gasto_b = D.gastos_do_mes(linhas, mes, "B")
        partes.append("🅱️ <b>CONTA DO PAI</b> · sem semana aberta")
        if gasto_b:
            partes.append(_bloco([f"{'lançado':<11} {D.num(gasto_b):>9}  esperando fechar"]))
        partes.append("<i>me fala quando ele mandar que eu fecho e zero.</i>")

    acum = D.acumulado()
    if acum:
        rotulo = "💰 guardado" if acum > 0 else "🔴 guardado"
        partes.append(f"{rotulo} <b>{D.brl(acum)}</b>")
    return "\n".join(partes)


# ---------------------------------------------------------------- pendências


def _texto_virada(ciclo, valor):
    """O que o bot fala quando você diz que seu pai mandou dinheiro."""
    partes = []
    fechado = ciclo.get("_fechado")

    if fechado:
        sobra = fechado["sobra"]
        partes.append("🔒 <b>SEMANA FECHADA</b>")
        partes.append(_bloco([
            f"{'gastou':<9} {D.num(fechado['gasto_final']):>9}",
            f"{'teto':<9} {D.num(fechado['teto']):>9}",
            f"{'sobra' if sobra >= 0 else 'estourou':<9} {D.num(abs(sobra)):>9}",
        ]))
        if sobra > 0:
            partes.append(f"💰 Guardei os {D.brl(sobra)} que sobraram.")
        elif sobra < 0:
            partes.append(f"⚠️ Estourou {D.brl(-sobra)} — tirei do guardado.")
        else:
            partes.append("Fechou redondinho, sem sobra.")

    acum = ciclo.get("_acumulado", 0)
    if fechado or acum:
        rotulo = "💰 <b>guardado</b>" if acum >= 0 else "🔴 <b>guardado</b>"
        partes.append(f"{rotulo} {D.brl(acum)}"
                      + ("" if acum >= 0 else " — essa diferença saiu do seu bolso."))

    partes.append(TRACO)
    partes.append(f"👨 <b>SEMANA NOVA</b> · teto {D.brl(valor)}, gasto zerado.")
    return "\n".join(partes)


def abrir_pendencia(pend, tipo, pergunta, msg_id, extra=None):
    pend.setdefault("abertas", []).append({
        "id": uuid.uuid4().hex[:8],
        "tipo": tipo,
        "pergunta": pergunta,
        "criada": D.agora().isoformat(timespec="seconds"),
        "msg_id": msg_id,
        "dados": extra or {},
    })


def expirar_pendencias(pend):
    """Ignorou por 24h? Some da frente e vai pro revisar.csv. Sem insistir."""
    limite = D.agora() - timedelta(hours=24)
    vivas = []
    for p in pend.get("abertas", []):
        if datetime.fromisoformat(p["criada"]) < limite:
            D.registrar_revisao(f"pendência expirada ({p['tipo']})", p["pergunta"])
        else:
            vivas.append(p)
    pend["abertas"] = vivas


# ---------------------------------------------------------------- despacho


class Sessao:
    def __init__(self, tg, api_key):
        self.tg = tg
        self.api_key = api_key
        self.linhas = D.ler_lancamentos()
        self.orcamento = D.ler_orcamento()
        self.pend = D.ler_pendencias()
        self.novos = []
        self.respostas = []
        self.mudou = False

    # ------------------------------------------------------------ grava

    def gravar(self, item, transcricao, msg_id, idx=0):
        # A chave é mensagem+posição, não mensagem+descrição: "gastei 20 no Uber e mais
        # 20 no Uber" são dois lançamentos legítimos e idênticos.
        marca = f"{msg_id}#{idx}" if msg_id else ""
        if marca and any(l["msg_id"] == marca for l in self.linhas):
            return None  # já processado num run anterior que morreu depois de gravar
        l = D.novo_lancamento(
            self.linhas,
            valor=item.get("valor"),
            tipo=item.get("tipo") or "gasto",
            conta=item.get("conta"),
            conta_origem=item.get("conta_origem"),
            categoria=item.get("categoria"),
            descricao=item.get("descricao"),
            data=item.get("data"),
            pagamento=item.get("pagamento"),
            confianca=item.get("confianca"),
            transcricao=transcricao,
            msg_id=marca,
        )
        if l["valor"] <= 0:
            D.registrar_revisao("valor zerado ou negativo", transcricao)
            return None

        self.linhas.append(l)
        self.novos.append(l)
        self.mudou = True

        # Receita na conta B abre uma semana nova, com o teto igual ao que ele mandou.
        if l["tipo"] == "receita" and l["conta"] == "B":
            ciclo = D.abrir_ciclo(l["valor"], l["data"], self.linhas)
            self.respostas.append(_texto_virada(ciclo, l["valor"]))
        return l

    # ------------------------------------------------------------ intenções

    def gasto(self, out, msg_id, base=0):
        """Um áudio pode trazer vários lançamentos — cada um com a sua própria conta."""
        for idx, item in enumerate(out.get("lancamentos", []), start=base):
            if (item.get("confianca") == "baixa") and not item.get("valor"):
                D.registrar_revisao("confiança baixa sem valor", out.get("transcricao", ""))
                continue
            self.gravar(item, out.get("transcricao", ""), msg_id, idx)

    def correcao(self, out, msg_id):
        alvo = out.get("alvo") or {}
        alvo_id, campo, novo = str(alvo.get("id") or ""), alvo.get("campo"), alvo.get("valor_novo")
        linha = next((l for l in self.linhas if l["id"] == alvo_id), None)
        if not linha or campo not in ("valor", "categoria", "conta", "descricao", "data"):
            self.respostas.append("🤔 Não achei qual lançamento corrigir. Me diz de novo?")
            return
        antigo = linha[campo]
        linha[campo] = round(float(novo), 2) if campo == "valor" else novo
        if campo in ("data", "valor") and linha["pagamento"] == "credito":
            linha["fatura"] = D.fatura_de(date.fromisoformat(linha["data"]))
        if campo == "conta":
            linha["conta_origem"] = "dito"
        self.mudou = True
        self.respostas.append(f"✏️ Corrigido: {campo} de <s>{antigo}</s> para <b>{linha[campo]}</b>.")

    def exclusao(self, out, msg_id):
        alvo = str((out.get("alvo") or {}).get("id") or "")
        linha = next((l for l in self.linhas if l["id"] == alvo), None)
        if not linha:
            self.respostas.append("🤔 Qual lançamento você quer apagar?")
            return
        # Única ação destrutiva do sistema — nunca apaga sem um "sim".
        abrir_pendencia(self.pend, "exclusao",
                        f"🗑 Apagar {D.brl(linha['valor'])} — {linha['categoria']} — "
                        f"{linha['descricao']} [{linha['conta']}]? (sim/não)",
                        msg_id, {"id": alvo})
        self.respostas.append(self.pend["abertas"][-1]["pergunta"])

    def orcamento_(self, out, msg_id):
        alvo = out.get("alvo") or {}
        categoria, valor = alvo.get("campo"), alvo.get("valor_novo")
        if categoria not in D.CATEGORIAS or valor is None:
            self.respostas.append("🤔 Qual categoria e qual valor esperado?")
            return
        D.definir_esperado(self.orcamento, categoria, float(valor))
        D.gravar_orcamento(self.orcamento)
        self.mudou = True
        self.respostas.append(f"🎯 Esperado de <b>{categoria}</b> agora é {D.brl(float(valor))}.")

    def recorrente(self, out, msg_id):
        alvo = out.get("alvo") or {}
        rec = D.ler_recorrentes()
        nome = alvo.get("campo") or "novo recorrente"
        item = {
            "nome": nome,
            "categoria": next((l.get("categoria") for l in out.get("lancamentos", [])), "Outros"),
            "valor": float(alvo.get("valor_novo") or
                           next((l.get("valor") for l in out.get("lancamentos", [])), 0)),
            "dia_limite": 20,
        }
        rec.setdefault("itens", [])
        rec["itens"] = [i for i in rec["itens"] if i["nome"] != nome] + [item]
        D.gravar_recorrentes(rec)
        self.mudou = True
        self.respostas.append(
            f"🔁 <b>{item['nome']}</b> ({D.brl(item['valor'])}) marcado como recorrente. "
            f"Não vou lançar sozinho — se passar do dia {item['dia_limite']} sem aparecer, eu pergunto."
        )

    def confirmacao(self, out, msg_id, texto):
        if not self.pend.get("abertas"):
            return False
        p = self.pend["abertas"][0]

        if p["tipo"] == "exclusao":
            if afirmativo(texto):
                self.linhas = [l for l in self.linhas if l["id"] != p["dados"]["id"]]
                self.mudou = True
                self.respostas.append("🗑 Apagado.")
            else:
                self.respostas.append("👍 Deixei como estava.")
            self.pend["abertas"].pop(0)
            return True

        if p["tipo"] == "recorrente_cobranca":
            if afirmativo(texto):
                self.gravar({
                    "valor": p["dados"]["valor"], "tipo": "gasto", "conta": "A",
                    "conta_origem": "padrao_categoria", "categoria": p["dados"]["categoria"],
                    "descricao": p["dados"]["nome"], "data": D.hoje().isoformat(),
                    "pagamento": "credito", "confianca": "alta",
                }, f"confirmado: {p['dados']['nome']}", msg_id)
            else:
                self.respostas.append("👍 Não lancei.")
            self.pend["abertas"].pop(0)
            return True

        if p["tipo"] == "revisao":
            if afirmativo(texto):
                for s in p["dados"]["sugestoes"]:
                    D.definir_esperado(self.orcamento, s["categoria"], s["esperado_sugerido"])
                D.gravar_orcamento(self.orcamento)
                self.mudou = True
                self.respostas.append("🎯 Esperados atualizados.")
            else:
                self.respostas.append("👍 Mantive os esperados como estavam.")
            self.pend["abertas"].pop(0)
            return True

        # Pendência de valor ou de conta: o Gemini já viu a pergunta no contexto e
        # devolveu o lançamento completo.
        if out.get("lancamentos"):
            self.gasto(out, msg_id)
            self.pend["abertas"].pop(0)
            return True
        if negativo(texto):
            self.pend["abertas"].pop(0)
            self.respostas.append("👍 Beleza, deixa pra lá.")
            return True
        return False

    # ------------------------------------------------------------ mensagem

    def processar(self, msg):
        msg_id = msg.get("message_id")
        file_id, mime = extrair_midia(msg)
        texto = msg.get("text") or msg.get("caption") or ""
        if not file_id and not texto.strip():
            return

        sem = contexto_semana(self.linhas)
        pendencia = self.pend["abertas"][0]["pergunta"] if self.pend.get("abertas") else None

        kwargs = dict(
            orcamento=self.orcamento, recentes=recentes(self.linhas), semana=sem,
            pendencia=pendencia, fatura=D.fatura_aberta(),
            recorrentes=D.ler_recorrentes(),
        )
        if file_id:
            kwargs["audio"] = self.tg.baixar(file_id, D.LIMITE_AUDIO)
            kwargs["mime"] = mime
        else:
            kwargs["texto"] = texto[:4000]

        out = extrator.extrair(self.api_key, **kwargs)
        transcricao = out.get("transcricao") or texto
        intencao = out.get("intencao")

        # Resposta a uma pergunta aberta tem prioridade — mas só se for mesmo uma
        # resposta. Um gasto novo e sem relação não pode engolir a pergunta pendente.
        respondendo = (intencao == "confirmacao"
                       or afirmativo(transcricao) or negativo(transcricao))
        if self.pend.get("abertas") and respondendo and self.confirmacao(out, msg_id, transcricao):
            return

        if out.get("precisa_perguntar") and out.get("pergunta"):
            abrir_pendencia(self.pend, "duvida", out["pergunta"], msg_id,
                            {"transcricao": transcricao})
            self.tg.enviar(f"❓ {out['pergunta']}", responder_a=msg_id)
            return

        if intencao in ("gasto", "receita"):
            self.gasto(out, msg_id)
        elif intencao == "consulta":
            self.respostas.append(responder_consulta(self.linhas, out.get("consulta")))
        elif intencao == "fatura":
            self.respostas.append(responder_fatura(self.linhas))
        elif intencao == "correcao":
            self.correcao(out, msg_id)
        elif intencao == "exclusao":
            self.exclusao(out, msg_id)
        elif intencao == "orcamento":
            self.orcamento_(out, msg_id)
        elif intencao == "recorrente":
            self.recorrente(out, msg_id)
        elif intencao == "relatorio":
            self.respostas.append("__RELATORIO__")
        # intenção "nenhuma": silêncio total, de propósito.


# ---------------------------------------------------------------- avisos


def avisos(sessao, state):
    """Lembretes que só valem se chegarem na hora certa. Cada um dispara uma vez só."""
    vistos = state.setdefault("avisos", {})
    fora = []
    hoje = D.hoje()

    def uma_vez(chave, texto):
        if chave not in vistos:
            vistos[chave] = hoje.isoformat()
            fora.append(texto)

    fecha, dias_f = D.fecha_em()
    if dias_f <= 2:
        total = D.total_fatura(sessao.linhas, D.fatura_aberta())
        uma_vez(f"fecha-{fecha}",
                f"⏳ Fatura fecha em {dias_f} dia(s) com {D.brl(total)}. "
                f"Compra a partir do dia {D.FECHAMENTO + 1} só é paga no mês seguinte.")

    vence, dias_v = D.vence_em()
    if dias_v <= 2:
        uma_vez(f"vence-{vence}", f"💳 Fatura vence em {dias_v} dia(s) — {vence.strftime('%d/%m')}.")

    # Recorrente que não apareceu: pergunta uma vez, nunca lança sozinho.
    mes = hoje.isoformat()[:7]
    if not sessao.pend.get("abertas"):
        for item in D.ler_recorrentes().get("itens", []):
            if hoje.day < item.get("dia_limite", 20):
                continue
            achou = any(
                l["categoria"] == item["categoria"] and l["data"][:7] == mes
                and abs(l["valor"] - item["valor"]) < max(2.0, item["valor"] * 0.2)
                for l in sessao.linhas
            )
            if achou:
                continue
            chave = f"rec-{item['nome']}-{mes}"
            if chave in vistos:
                continue
            vistos[chave] = hoje.isoformat()
            abrir_pendencia(sessao.pend, "recorrente_cobranca",
                            f"🔁 {item['nome']} ({D.brl(item['valor'])}) ainda não apareceu "
                            f"esse mês — pagou?", None, item)
            fora.append(sessao.pend["abertas"][-1]["pergunta"])
            break

    ciclo = D.ciclo_aberto()
    if D.semana_passou(ciclo) and not sessao.pend.get("abertas"):
        chave = f"semana-{ciclo['inicio'] if ciclo else 'inicial'}"
        uma_vez(chave, "👨 Faz mais de uma semana desde a última entrada — seu pai mandou?")

    return fora


def revisao_mensal(sessao, state, api_key):
    """Todo dia 1º a IA propõe ajuste nos esperados. Propõe — quem aprova é ele."""
    hoje = D.hoje()
    mes = hoje.isoformat()[:7]
    if hoje.day != 1 or state.setdefault("avisos", {}).get(f"revisao-{mes}"):
        return None
    state["avisos"][f"revisao-{mes}"] = hoje.isoformat()

    historico = {}
    for i in range(1, 7):
        m = (hoje.replace(day=1) - timedelta(days=i * 28)).isoformat()[:7]
        cats = D.por_categoria(sessao.linhas, m, "A")
        if cats:
            historico[m] = cats
    if len(historico) < 3:
        return None

    try:
        r = extrator.revisar_orcamento(api_key, D.esperado_por_categoria(sessao.orcamento), historico)
    except Exception:
        return None
    if not r.get("sugestoes"):
        return None

    linhas = ["🔎 <b>Revisão do mês</b>", r.get("resumo", "")]
    for s in r["sugestoes"]:
        linhas.append(f"• {s['categoria']}: {D.brl(s['esperado_atual'])} → "
                      f"<b>{D.brl(s['esperado_sugerido'])}</b> — {s['motivo']}")
    linhas.append("\nAplico esses ajustes? (sim/não)")
    abrir_pendencia(sessao.pend, "revisao", "\n".join(linhas), None, r)
    return "\n".join(linhas)


# ---------------------------------------------------------------- main


def avisar_uma_vez(tg, state, erro):
    """Mesmo erro 34 vezes por dia vira ruído — avisa uma vez a cada 6h."""
    if not erro.aviso:
        return
    vistos = state.setdefault("erros", {})
    agora = D.agora()
    anterior = vistos.get(erro.chave)
    if anterior and (agora - datetime.fromisoformat(anterior)).total_seconds() < 6 * 3600:
        return
    vistos[erro.chave] = agora.isoformat(timespec="seconds")
    tg.enviar(erro.aviso)


def limpar_erro(state, chave):
    state.setdefault("erros", {}).pop(chave, None)


def coletar(tg, state):
    """Baixa o que chegou e joga na fila persistida ANTES de processar.

    O Telegram descarta mensagem não lida depois de 24h. A fila mora no git, então
    mesmo que o Gemini fique fora do ar dois dias, nada se perde.
    """
    fila = D.ler_fila()
    offset = state.get("offset", 0)
    try:
        updates = tg.updates(offset)
    except erros.ErroSistema as e:
        avisar_uma_vez(tg, state, e)
        return fila, 0  # sem novidade: processa o que já estava na fila

    chat_id = tg.chat_id
    novos = 0
    for u in sorted(updates, key=lambda x: x["update_id"]):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue
        if msg.get("from", {}).get("is_bot"):
            continue
        fila.setdefault("pendentes", []).append({"msg": msg, "tentativas": 0})
        novos += 1

    D.gravar_fila(fila)
    D.gravar_state(state)
    return fila, novos


def processar_fila(sessao, fila, state):
    """Em ordem. Falha temporária para tudo; permanente descarta só o item."""
    restantes, parou = [], None

    for i, item in enumerate(fila.get("pendentes", [])):
        if parou:
            restantes.append(item)
            continue
        # O free tier do Gemini limita requisições por minuto. Espaçar aqui evita
        # tomar 429 quando chega uma rajada de áudios de uma vez.
        if i:
            time.sleep(PAUSA_ENTRE_ITENS)
        try:
            sessao.processar(item["msg"])
            for chave in ("gemini-rpm", "gemini-cota-dia", "gemini-instavel"):
                limpar_erro(state, chave)
        except erros.ErroTemporario as e:
            # Nada de perder a mensagem: ela volta pro topo da fila.
            item["tentativas"] = item.get("tentativas", 0) + 1
            restantes.append(item)
            parou = e
        except erros.ErroPermanente as e:
            D.registrar_revisao(str(e), _texto_de(item["msg"]))
            avisar_uma_vez(sessao.tg, state, e)
        except Exception as e:
            traceback.print_exc()
            item["tentativas"] = item.get("tentativas", 0) + 1
            if item["tentativas"] >= D.MAX_TENTATIVAS:
                # Item envenenado não pode travar a fila pra sempre.
                D.registrar_revisao(f"falhou {item['tentativas']}x: {e}", _texto_de(item["msg"]))
                sessao.tg.enviar(
                    "⚠️ Uma mensagem falhou 3 vezes e eu desisti dela. "
                    "Anotei em <code>revisar.csv</code> — se era gasto, manda de novo.",
                    responder_a=item["msg"].get("message_id"),
                )
            else:
                restantes.append(item)

    fila["pendentes"] = restantes
    return parou


def _texto_de(msg):
    return msg.get("text") or msg.get("caption") or f"[áudio msg {msg.get('message_id')}]"


def main():
    faltando = [v for v in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")
                if not os.environ.get(v)]
    if faltando:
        print(f"faltam os secrets: {', '.join(faltando)}", file=sys.stderr)
        return 2

    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    api_key = os.environ["GEMINI_API_KEY"]
    completo = os.environ.get("RELATORIO") == "1"

    tg = Telegram(token, chat_id)
    state = D.ler_state()

    inicio = time.monotonic()
    ultima_atividade = inicio
    sem_progresso = 0
    passada = 0

    while True:
        passada += 1
        sessao = Sessao(tg, api_key)
        expirar_pendencias(sessao.pend)

        fila, novos = coletar(tg, state)
        antes = len(fila["pendentes"])
        parou = processar_fila(sessao, fila, state)
        restam = len(fila["pendentes"])

        if sessao.mudou:
            D.gravar_lancamentos(sessao.linhas)
        state["ultimo_run"] = D.agora().isoformat(timespec="seconds")

        saida = [r for r in sessao.respostas if r != "__RELATORIO__"]
        pediu_painel = "__RELATORIO__" in sessao.respostas
        relatorio = completo and passada == 1  # o painel completo vai uma vez só

        if parou:
            avisar_uma_vez(tg, state, parou)
            if restam:
                # O motivo vai SEMPRE junto, mesmo com o aviso detalhado suprimido pela
                # regra de não repetir: "5 esperando" sem dizer por quê é falha silenciosa.
                saida.append(f"📥 <b>{restam} mensagem(ns) na fila</b> — não perdi nenhuma.\n"
                             f"<i>Travou em: {MOTIVO.get(parou.chave, parou.chave)}. "
                             f"Tento de novo sozinho.</i>")
        else:
            # Só faz sentido cobrar recorrente e revisar orçamento com a fila em dia.
            saida += avisos(sessao, state)
            rev = revisao_mensal(sessao, state, api_key)
            if rev:
                saida.append(rev)

        D.gravar_fila(fila)
        D.gravar_pendencias(sessao.pend)
        D.gravar_state(state)

        if sessao.mudou or relatorio or pediu_painel:
            try:
                painel.gerar(sessao.linhas, sessao.orcamento)
            except Exception:
                traceback.print_exc()  # painel quebrado não pode invalidar o ledger

        if sessao.novos or relatorio or pediu_painel:
            saida.append(montar_resumo(sessao.linhas, sessao.orcamento, sessao.novos))

        # Silêncio quando não houve nada: um bot que fala 34 vezes por dia você silencia.
        if saida:
            tg.enviar("\n".join(saida))
        if relatorio or pediu_painel:
            tg.documento(painel.SAIDA, "📊 Painel atualizado")

        print(f"passada={passada} novos={novos} fila={restam} "
              f"lancados={len(sessao.novos)} offset={state['offset']} parou={parou}")

        if not CICLO_RAPIDO:
            break

        agora = time.monotonic()
        if novos or sessao.novos:
            ultima_atividade = agora
        # Fila que não encolhe e sem mensagem nova = não adianta insistir de perto.
        sem_progresso = 0 if (novos or restam < antes) else sem_progresso + 1

        if not (novos or restam):
            break  # nada acontecendo: volta pro ritmo de 30 min
        if sem_progresso >= MAX_SEM_PROGRESSO:
            break  # provável indisponibilidade: o próximo ciclo tenta de novo
        if agora - ultima_atividade > JANELA_ATIVA:
            break  # você parou de lançar
        if agora - inicio + CICLO_RAPIDO > TEMPO_MAX:
            break  # teto duro, pra não comer o orçamento de minutos

        time.sleep(CICLO_RAPIDO)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        traceback.print_exc()
        # Último recurso: o run morreu de um jeito que nem a fila previu.
        try:
            Telegram(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]).enviar(
                f"🔴 <b>O sync quebrou.</b>\n<code>{type(e).__name__}: {str(e)[:200]}</code>\n"
                "Suas mensagens seguem guardadas. Dá uma olhada no GitHub Actions."
            )
        except Exception:
            pass
        sys.exit(1)
