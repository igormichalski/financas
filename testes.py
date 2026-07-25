#!/usr/bin/env python3
"""Testes do sistema, sem tocar no Telegram nem no Gemini.

O extrator é substituído por respostas canônicas — o que está sob teste aqui é o
despacho, o roteamento A/B, o ciclo da fatura e o isolamento entre as carteiras.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import date

import dados as D

FALHAS = []


def checa(nome, condicao, detalhe=""):
    print(f"  {'✅' if condicao else '❌'} {nome}" + (f" — {detalhe}" if not condicao else ""))
    if not condicao:
        FALHAS.append(nome)


# ---------------------------------------------------------------- ambiente


def sandbox():
    """Redireciona todos os arquivos pra uma pasta temporária."""
    tmp = tempfile.mkdtemp(prefix="financas-teste-")
    for nome in ("LANCAMENTOS", "ORCAMENTO", "RECORRENTES", "SEMANAS",
                 "PENDENCIAS", "STATE", "REVISAR"):
        setattr(D, nome, os.path.join(tmp, getattr(D, nome).split(os.sep)[-1]))
    D.gravar_orcamento({
        "A": {"renda": 1600.0, "itens": [
            {"nome": "Comer fora", "categoria": "Restaurantes/Lanches", "esperado": 261.0},
            {"nome": "Academia", "categoria": "Saúde/Academia", "esperado": 110.0},
        ]},
        "B": {"teto_semanal": 350.0},
        "limiares": {"Combustível": 110.0, "Mercado/Supermercado": 160.0},
    })
    D.gravar_lancamentos([])
    D.gravar_semanas({"ciclos": []})
    D.gravar_pendencias({"abertas": []})
    D.gravar_recorrentes({"itens": [
        {"nome": "Academia", "categoria": "Saúde/Academia", "valor": 110.0, "dia_limite": 1},
    ]})
    return tmp


class FakeTelegram:
    def __init__(self, chat_id="-1"):
        self.chat_id = str(chat_id)
        self.enviadas = []
        self.docs = []

    def enviar(self, texto, responder_a=None):
        self.enviadas.append(texto)

    def documento(self, caminho, legenda=""):
        self.docs.append(caminho)

    def baixar(self, file_id, limite=0):
        return b"audio-falso"

    def updates(self, offset):
        return []


RESPOSTAS = {}


def fake_extrair(api_key, **kw):
    chave = kw.get("texto", "")
    if chave not in RESPOSTAS:
        raise AssertionError(f"teste sem resposta canônica para {chave!r}")
    return dict(RESPOSTAS[chave])


def msg(texto, mid=1):
    return {"message_id": mid, "text": texto, "chat": {"id": "-1"}, "from": {"is_bot": False}}


def lanc(valor, categoria, conta=None, origem="padrao_categoria", tipo="gasto",
         data=None, pagamento="nao_informado", descricao="x"):
    return {"valor": valor, "tipo": tipo, "conta": conta, "conta_origem": origem,
            "categoria": categoria, "descricao": descricao,
            "data": data or D.hoje().isoformat(), "pagamento": pagamento, "confianca": "alta"}


# ---------------------------------------------------------------- testes


def teste_fatura():
    print("\n💳 Ciclo da fatura (fecha 15, vence 22)")
    casos = [
        ("2026-07-14", "2026-07-22", "compra antes do fechamento paga no mesmo mês"),
        ("2026-07-15", "2026-07-22", "no dia do fechamento ainda entra"),
        ("2026-07-16", "2026-08-22", "um dia depois, um mês a mais"),
        ("2026-12-16", "2027-01-22", "virada de ano"),
    ]
    for d, esperado, nome in casos:
        obtido = D.fatura_de(date.fromisoformat(d))
        checa(f"{d} → {esperado} ({nome})", obtido == esperado, f"deu {obtido}")


def teste_roteamento(S):
    print("\n🅰️🅱️  Roteamento entre as carteiras")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "mercado": {"transcricao": "duzentos no leve max", "intencao": "gasto",
                    "precisa_perguntar": False,
                    "lancamentos": [lanc(200, "Mercado/Supermercado", descricao="Leve Max")]},
        "almoco": {"transcricao": "gastei 35 no almoço", "intencao": "gasto",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]},
        "gasolina_dito": {"transcricao": "abasteci 80, conta B", "intencao": "gasto",
                          "precisa_perguntar": False,
                          "lancamentos": [lanc(80, "Combustível", "B", "dito", descricao="posto")]},
        "viagem": {"transcricao": "abasteci trezentos pra viagem", "intencao": "gasto",
                   "precisa_perguntar": True,
                   "pergunta": "Combustível de R$ 300 — conta B ou é seu?",
                   "lancamentos": []},
        "ruido": {"transcricao": "depois te mando aquele arquivo", "intencao": "nenhuma",
                  "precisa_perguntar": False, "lancamentos": []},
    })

    s = S()
    for i, chave in enumerate(["mercado", "almoco", "gasolina_dito", "viagem", "ruido"], 1):
        s.processar(msg(chave, mid=i))

    por_desc = {l["descricao"]: l for l in s.novos}
    checa("mercado sem palavra-chave cai na conta B",
          por_desc.get("Leve Max", {}).get("conta") == "B")
    checa("almoço cai na conta A", por_desc.get("almoço", {}).get("conta") == "A")
    checa("'conta B' dito ganha do padrão",
          por_desc.get("posto", {}).get("conta") == "B"
          and por_desc["posto"]["conta_origem"] == "dito")
    checa("combustível de R$ 300 vira pergunta, não lançamento",
          len(s.novos) == 3 and len(s.pend["abertas"]) == 1,
          f"{len(s.novos)} lançados, {len(s.pend['abertas'])} pendências")
    checa("áudio sem gasto não gera lançamento nem pergunta",
          not any("arquivo" in (l["transcricao"] or "") for l in s.novos))
    checa("bot ficou calado no ruído",
          not any("arquivo" in t for t in s.tg.enviadas))
    return s


def teste_pergunta_resolve(S):
    print("\n❓ Loop de pergunta e resposta")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "vago": {"transcricao": "gastei uma grana no mercado", "intencao": "gasto",
                 "precisa_perguntar": True, "pergunta": "Quanto foi no mercado?",
                 "lancamentos": []},
        "120": {"transcricao": "120", "intencao": "confirmacao", "precisa_perguntar": False,
                "lancamentos": [lanc(120, "Mercado/Supermercado", descricao="mercado")]},
    })
    s = S()
    s.processar(msg("vago", 1))
    checa("pergunta aberta em vez de lançamento", len(s.pend["abertas"]) == 1 and not s.novos)
    # Um gasto novo, sem relação, não pode engolir a pergunta que está de pé.
    RESPOSTAS["outro"] = {"transcricao": "gastei 20 no uber", "intencao": "gasto",
                          "precisa_perguntar": False,
                          "lancamentos": [lanc(20, "Transporte/App", descricao="uber")]}
    s.processar(msg("outro", 2))
    checa("gasto novo não consome a pendência",
          len(s.pend["abertas"]) == 1 and len(s.novos) == 1)

    s.processar(msg("120", 3))
    checa("resposta fecha o lançamento com 120",
          any(l["valor"] == 120.0 for l in s.novos))
    checa("pendência sumiu", not s.pend["abertas"])


def teste_ciclo_semanal(S):
    print("\n👨 Ciclo semanal da conta B")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "pai350": {"transcricao": "meu pai mandou 350", "intencao": "receita",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(350, "Outros", "B", "dito", "receita",
                                        descricao="ajuda do pai")]},
        "mercado289": {"transcricao": "289 no mercado", "intencao": "gasto",
                       "precisa_perguntar": False,
                       "lancamentos": [lanc(289, "Mercado/Supermercado", descricao="mercado")]},
        "pai300": {"transcricao": "meu pai mandou 300", "intencao": "receita",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(300, "Outros", "B", "dito", "receita",
                                        descricao="ajuda do pai")]},
    })
    s = S()
    s.processar(msg("pai350", 1))
    ciclo = D.ciclo_aberto()
    checa("receita do pai abre semana com teto 350", ciclo and ciclo["teto"] == 350.0)

    s.processar(msg("mercado289", 2))
    sem = sync.contexto_semana(s.linhas)
    checa("restam R$ 61 da semana", abs(sem["resta"] - 61.0) < 0.01, f"deu {sem['resta']}")

    resumo = sync.montar_resumo(s.linhas, s.orcamento)
    checa("estouro da semana não aparece no bloco A",
          "1.600" in resumo and "R$ 0,00 de R$ 1.600,00" in resumo,
          "gasto da conta B vazou pro total do salário")

    s.processar(msg("pai300", 3))
    checa("semana nova tem teto 300 (o que ele mandou, não os 350 teóricos)",
          D.ciclo_aberto()["teto"] == 300.0)


def teste_fechamento(S):
    print("\n🔒 Fechar a semana e guardar a sobra")
    import sync
    from datetime import timedelta

    D.gravar_semanas({"ciclos": [], "acumulado": 0.0})
    ontem = (D.hoje() - timedelta(days=2)).isoformat()

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "antes": {"transcricao": "60 no mercado", "intencao": "gasto",
                  "precisa_perguntar": False,
                  "lancamentos": [lanc(60, "Mercado/Supermercado", data=ontem, descricao="antes")]},
        "pai350": {"transcricao": "meu pai mandou 350", "intencao": "receita",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(350, "Outros", "B", "dito", "receita", descricao="pai")]},
        "gasta289": {"transcricao": "289 no mercado", "intencao": "gasto",
                     "precisa_perguntar": False,
                     "lancamentos": [lanc(289, "Mercado/Supermercado", descricao="semana1")]},
        "pai300": {"transcricao": "meu pai mandou 300", "intencao": "receita",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(300, "Outros", "B", "dito", "receita", descricao="pai")]},
        "gasta400": {"transcricao": "400 no mercado", "intencao": "gasto",
                     "precisa_perguntar": False,
                     "lancamentos": [lanc(400, "Mercado/Supermercado", descricao="semana2")]},
        "pai350b": {"transcricao": "meu pai mandou 350", "intencao": "receita",
                    "precisa_perguntar": False,
                    "lancamentos": [lanc(350, "Outros", "B", "dito", "receita", descricao="pai")]},
    })

    s = S()
    s.processar(msg("antes", 1))
    checa("lança antes de existir teto, sem ciclo", sync.contexto_semana(s.linhas) is None)

    s.processar(msg("pai350", 2))
    sem = sync.contexto_semana(s.linhas)
    checa("anunciar zera o gasto", sem["gasto"] == 0.0, f"contou {sem['gasto']}")
    checa("teto novo é o que ele mandou", sem["ciclo"]["teto"] == 350.0)
    checa("o que foi lançado antes fica registrado como período inicial",
          D.ler_semanas().get("periodo_inicial", {}).get("gasto") == 60.0)
    checa("período inicial não entra no guardado", D.acumulado() == 0.0)

    s.processar(msg("gasta289", 3))
    checa("gasto da semana desconta do teto",
          sync.contexto_semana(s.linhas)["resta"] == 61.0)

    s.processar(msg("pai300", 4))
    checa("fechar guarda a sobra de R$ 61", D.acumulado() == 61.0, f"{D.acumulado()}")
    checa("semana nova nasce zerada", sync.contexto_semana(s.linhas)["gasto"] == 0.0)
    checa("teto acompanha o valor real (300, não 350)",
          sync.contexto_semana(s.linhas)["ciclo"]["teto"] == 300.0)
    checa("o bot avisa que fechou e guardou",
          any("Semana fechada" in r and "guardei separado" in r.lower() for r in s.respostas),
          str(s.respostas[-1:]))

    s.processar(msg("gasta400", 5))
    s.processar(msg("pai350b", 6))
    checa("estouro de R$ 100 sai do guardado (61 - 100)",
          D.acumulado() == -39.0, f"{D.acumulado()}")
    checa("avisa que estourou e que saiu do bolso",
          any("Estourou" in r and "bolso" in r for r in s.respostas), str(s.respostas[-1:]))

    fechados = [c for c in D.ler_semanas()["ciclos"] if c.get("fechado_em")]
    checa("semanas fechadas viram histórico com sobra registrada",
          len(fechados) == 2 and fechados[0]["sobra"] == 61.0 and fechados[1]["sobra"] == -100.0)
    checa("semana fechada não muda mais, mesmo com lançamento novo",
          D.gasto_no_ciclo(s.linhas, fechados[0]) == 289.0)


def teste_isolamento(S):
    print("\n🚧 Isolamento entre as carteiras")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "b500": {"transcricao": "500 no mercado, conta B", "intencao": "gasto",
                 "precisa_perguntar": False,
                 "lancamentos": [lanc(500, "Mercado/Supermercado", "B", "dito")]},
        "a100": {"transcricao": "100 no almoço", "intencao": "gasto",
                 "precisa_perguntar": False,
                 "lancamentos": [lanc(100, "Restaurantes/Lanches")]},
    })
    s = S()
    s.processar(msg("b500", 1))
    s.processar(msg("a100", 2))
    mes = D.hoje().isoformat()[:7]
    checa("conta A soma só os 100", D.gastos_do_mes(s.linhas, mes, "A") == 100.0,
          str(D.gastos_do_mes(s.linhas, mes, "A")))
    checa("conta B soma só os 500", D.gastos_do_mes(s.linhas, mes, "B") == 500.0)


def teste_intencoes(S):
    print("\n🎙 Comandos por voz")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "gasto": {"transcricao": "35 no almoço", "intencao": "gasto", "precisa_perguntar": False,
                  "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]},
        "consulta": {"transcricao": "quanto gastei de comer fora?", "intencao": "consulta",
                     "precisa_perguntar": False, "lancamentos": [],
                     "consulta": {"periodo": "mes", "categoria": "Restaurantes/Lanches",
                                  "conta": "A"}},
        "corrige": {"transcricao": "aquele almoço foi 45", "intencao": "correcao",
                    "precisa_perguntar": False, "lancamentos": [],
                    "alvo": {"id": "1", "campo": "valor", "valor_novo": "45"}},
        "orcamento": {"transcricao": "meu esperado de comer fora é 300", "intencao": "orcamento",
                      "precisa_perguntar": False, "lancamentos": [],
                      "alvo": {"campo": "Restaurantes/Lanches", "valor_novo": "300"}},
        "apaga": {"transcricao": "apaga o último", "intencao": "exclusao",
                  "precisa_perguntar": False, "lancamentos": [], "alvo": {"id": "1"}},
        "sim": {"transcricao": "sim", "intencao": "confirmacao", "precisa_perguntar": False,
                "lancamentos": []},
        "fatura": {"transcricao": "quanto tá a fatura?", "intencao": "fatura",
                   "precisa_perguntar": False, "lancamentos": []},
    })

    s = S()
    s.processar(msg("gasto", 1))
    s.processar(msg("consulta", 2))
    checa("consulta responde com o número real",
          any("35,00" in r for r in s.respostas), str(s.respostas[-1:]))

    s.processar(msg("corrige", 3))
    checa("correção muda o valor pra 45",
          any(l["id"] == "1" and l["valor"] == 45.0 for l in s.linhas))

    s.processar(msg("orcamento", 4))
    checa("esperado vira 300",
          D.esperado_por_categoria(D.ler_orcamento())["Restaurantes/Lanches"] == 300.0)

    antes = len(s.linhas)
    s.processar(msg("apaga", 5))
    checa("exclusão não apaga sem confirmar", len(s.linhas) == antes and s.pend["abertas"])
    s.processar(msg("sim", 6))
    checa("depois do 'sim' apaga", len(s.linhas) == antes - 1)

    s.processar(msg("fatura", 7))
    checa("responde sobre a fatura", any("Fatura aberta" in r for r in s.respostas))


def teste_idempotencia(S):
    print("\n🔁 Idempotência")
    RESPOSTAS.clear()
    RESPOSTAS["dup"] = {"transcricao": "35 no almoço", "intencao": "gasto",
                        "precisa_perguntar": False,
                        "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]}
    s = S()
    s.processar(msg("dup", 99))
    s.processar(msg("dup", 99))
    checa("mesma mensagem processada 2x não duplica", len(s.novos) == 1, f"{len(s.novos)} novos")


def teste_recorrente(S):
    print("\n🔔 Cobrança de recorrente")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS["sim"] = {"transcricao": "sim", "intencao": "confirmacao",
                        "precisa_perguntar": False, "lancamentos": []}

    # O extrator recebe os recorrentes no contexto — é o que faz "yt pago" virar
    # R$ 26,90 sem perguntar o valor.
    vistos = {}
    original = __import__("extrator").extrair

    def espiao(api_key, **kw):
        vistos.update(kw)
        return original(api_key, **kw)

    import extrator
    extrator.extrair = espiao
    RESPOSTAS["yt"] = {"transcricao": "yt pago", "intencao": "gasto",
                       "precisa_perguntar": False,
                       "lancamentos": [lanc(26.9, "Streaming", descricao="YouTube Premium")]}
    s0 = S()
    s0.processar(msg("yt", 90))
    checa("os recorrentes cadastrados chegam no contexto do extrator",
          bool((vistos.get("recorrentes") or {}).get("itens")))
    checa("'yt pago' lança sem perguntar valor",
          len(s0.novos) == 1 and s0.novos[0]["valor"] == 26.9 and not s0.pend["abertas"])
    extrator.extrair = original

    s = S()
    state = {"avisos": {}}
    saida = sync.avisos(s, state)
    checa("academia não apareceu no mês → pergunta uma vez",
          any("Academia" in t for t in saida) and len(s.pend["abertas"]) == 1)

    saida2 = sync.avisos(s, state)
    checa("não pergunta de novo no run seguinte", not any("Academia" in t for t in saida2))

    s.processar(msg("sim", 1))
    checa("o 'sim' lança os 110", any(l["valor"] == 110.0 for l in s.novos))


def teste_multi_lancamento(S):
    print("\n🎯 Vários lançamentos num áudio só")
    RESPOSTAS.clear()
    RESPOSTAS.update({
        "misto": {"transcricao": "gastei 40 de mercado na conta B e 23 de restaurante",
                  "intencao": "gasto", "precisa_perguntar": False, "lancamentos": [
                      lanc(40, "Mercado/Supermercado", "B", "dito", descricao="mercado"),
                      lanc(23, "Restaurantes/Lanches", descricao="restaurante"),
                  ]},
        "tres": {"transcricao": "Igor, Kauan e Bruno me mandaram 5 reais ontem",
                 "intencao": "receita", "precisa_perguntar": False, "lancamentos": [
                     lanc(5, "Outros", "A", "dito", "receita", descricao="Igor"),
                     lanc(5, "Outros", "A", "dito", "receita", descricao="Kauan"),
                     lanc(5, "Outros", "A", "dito", "receita", descricao="Bruno"),
                 ]},
        "iguais": {"transcricao": "gastei 20 no uber e mais 20 no uber", "intencao": "gasto",
                   "precisa_perguntar": False, "lancamentos": [
                       lanc(20, "Transporte/App", descricao="uber"),
                       lanc(20, "Transporte/App", descricao="uber"),
                   ]},
    })

    s = S()
    s.processar(msg("misto", 1))
    checa("um áudio vira 2 lançamentos", len(s.novos) == 2, f"{len(s.novos)}")
    contas = {l["descricao"]: l["conta"] for l in s.novos}
    checa("cada item mantém a sua própria conta",
          contas.get("mercado") == "B" and contas.get("restaurante") == "A", str(contas))

    antes = len(s.novos)
    ciclos_antes = len(D.ler_semanas().get("ciclos", []))
    s.processar(msg("tres", 2))
    tres = s.novos[antes:]
    checa("3 pessoas + 1 valor = 3 receitas de R$ 5",
          len(tres) == 3 and all(l["valor"] == 5.0 and l["tipo"] == "receita" for l in tres),
          f"{len(tres)} itens")
    checa("receita de amigo vai pra conta A, não abre semana do pai",
          all(l["conta"] == "A" for l in tres)
          and len(D.ler_semanas().get("ciclos", [])) == ciclos_antes,
          "abriu ciclo indevidamente")

    antes = len(s.novos)
    s.processar(msg("iguais", 3))
    checa("dois lançamentos idênticos não são deduplicados",
          len(s.novos) - antes == 2, f"{len(s.novos) - antes}")

    s.processar(msg("iguais", 3))
    checa("mas reprocessar a MESMA mensagem ainda não duplica",
          len(s.novos) - antes == 2, f"{len(s.novos) - antes}")

    mes = D.hoje().isoformat()[:7]
    resumo = __import__("sync").montar_resumo(s.linhas, s.orcamento)
    checa("receita entra na sobra do mês",
          D.receitas_do_mes(s.linhas, mes, "A") == 15.0 and "1.615,00" in resumo,
          f"receitas={D.receitas_do_mes(s.linhas, mes, 'A')}")


def teste_erros(S):
    print("\n🛡  Casos de borda e falhas")
    import erros
    import sync

    # --- classificação ---
    casos = [
        ("limite por minuto", erros.classificar_gemini(429, '{"error":{"status":"RESOURCE_EXHAUSTED"}}'),
         erros.ErroTemporario, "gemini-rpm"),
        ("cota diária", erros.classificar_gemini(429, "quota GenerateRequestsPerDayPerProject"),
         erros.ErroTemporario, "gemini-cota-dia"),
        ("chave inválida", erros.classificar_gemini(400, "API_KEY_INVALID"),
         erros.ErroPermanente, "gemini-chave"),
        ("Gemini fora do ar", erros.classificar_gemini(503, "overloaded"),
         erros.ErroTemporario, "gemini-instavel"),
        ("token do bot ruim", erros.classificar_telegram("getUpdates", 401, ""),
         erros.ErroPermanente, "tg-token"),
        ("bot tirado do grupo", erros.classificar_telegram("sendMessage", 403, ""),
         erros.ErroPermanente, "tg-acesso"),
        ("flood do Telegram", erros.classificar_telegram("sendMessage", 429, "retry_after: 12"),
         erros.ErroTemporario, "tg-flood"),
        ("arquivo grande", erros.classificar_telegram("getFile", 400, "file is too big"),
         erros.ErroPermanente, "tg-arquivo-grande"),
        ("filtro de conteúdo", erros.erro_sem_resposta("SAFETY"),
         erros.ErroPermanente, "gemini-safety"),
        ("sem internet", erros.erro_rede("Gemini", "timeout"),
         erros.ErroTemporario, "rede-Gemini"),
    ]
    for nome, erro, tipo, chave in casos:
        checa(f"{nome} → {tipo.__name__}",
              isinstance(erro, tipo) and erro.chave == chave, f"{type(erro).__name__}/{erro.chave}")
    checa("todo erro temporário tem aviso em português",
          all(e.aviso for _, e, t, _ in casos if t is erros.ErroTemporario))

    # --- a fila não perde mensagem quando o Gemini cai ---
    RESPOSTAS.clear()
    RESPOSTAS["ok"] = {"transcricao": "35 no almoço", "intencao": "gasto",
                       "precisa_perguntar": False,
                       "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]}

    def cai(api_key, **kw):
        if kw.get("texto") == "quebra":
            raise erros.ErroTemporario("cota", "🟡 cota", chave="gemini-rpm")
        return fake_extrair(api_key, **kw)

    import extrator
    extrator.extrair = cai
    s, state = S(), {"avisos": {}, "erros": {}}
    fila = {"pendentes": [{"msg": msg("ok", 1), "tentativas": 0},
                          {"msg": msg("quebra", 2), "tentativas": 0},
                          {"msg": msg("ok", 3), "tentativas": 0}]}
    parou = sync.processar_fila(s, fila, state)
    checa("erro temporário para a fila", isinstance(parou, erros.ErroTemporario))
    checa("o que veio antes da falha foi gravado", len(s.novos) == 1)
    checa("a mensagem que falhou e as seguintes ficam na fila",
          len(fila["pendentes"]) == 2, f"{len(fila['pendentes'])} na fila")
    checa("ordem preservada — a que falhou volta na frente",
          fila["pendentes"][0]["msg"]["text"] == "quebra")
    checa("tentativa foi contada", fila["pendentes"][0]["tentativas"] == 1)

    # --- erro permanente descarta só o item e segue ---
    def permanente(api_key, **kw):
        if kw.get("texto") == "ruim":
            raise erros.ErroPermanente("bloqueado", "🔴 bloqueado", chave="gemini-safety")
        return fake_extrair(api_key, **kw)

    extrator.extrair = permanente
    s2, state2 = S(), {"avisos": {}, "erros": {}}
    fila2 = {"pendentes": [{"msg": msg("ruim", 1), "tentativas": 0},
                           {"msg": msg("ok", 2), "tentativas": 0}]}
    parou2 = sync.processar_fila(s2, fila2, state2)
    checa("erro permanente não trava a fila", parou2 is None and not fila2["pendentes"])
    checa("o item seguinte foi processado normalmente", len(s2.novos) == 1)

    # --- item envenenado sai depois de 3 tentativas ---
    def sempre_quebra(api_key, **kw):
        raise ValueError("bug inesperado")

    extrator.extrair = sempre_quebra
    s3, state3 = S(), {"avisos": {}, "erros": {}}
    fila3 = {"pendentes": [{"msg": msg("x", 1), "tentativas": 2}]}
    sync.processar_fila(s3, fila3, state3)
    checa("item que falha 3x é descartado com aviso",
          not fila3["pendentes"] and any("desisti" in t for t in s3.tg.enviadas))

    extrator.extrair = fake_extrair

    # --- aviso não repete ---
    tg = FakeTelegram()
    st = {"erros": {}}
    e = erros.ErroTemporario("cota", "🟡 cota estourou", chave="gemini-rpm")
    sync.avisar_uma_vez(tg, st, e)
    sync.avisar_uma_vez(tg, st, e)
    checa("mesmo erro não vira spam", len(tg.enviadas) == 1, f"{len(tg.enviadas)} avisos")

    # --- secrets faltando ---
    guardado = {k: os.environ.pop(k, None) for k in
                ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")}
    checa("sem secrets, sai com código 2 em vez de estourar", sync.main() == 2)
    for k, v in guardado.items():
        if v:
            os.environ[k] = v


def teste_cadencia(S):
    print("\n⏱  Cadência adaptativa (3 min com movimento, 30 min parado)")
    import sync

    guardado = {k: os.environ.get(k) for k in ("CICLO_RAPIDO", "JANELA_ATIVA", "TEMPO_MAX")}
    sync.CICLO_RAPIDO = 0.01
    sync.JANELA_ATIVA = 999
    sync.TEMPO_MAX = 999
    sync.MAX_SEM_PROGRESSO = 2

    RESPOSTAS.clear()
    RESPOSTAS["ok"] = {"transcricao": "35 no almoço", "intencao": "gasto",
                       "precisa_perguntar": False,
                       "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]}

    class TgRoteiro(FakeTelegram):
        """Entrega mensagens em levas, uma leva por passada."""

        def __init__(self, levas):
            super().__init__()
            self.levas = list(levas)
            self.passadas = 0

        def updates(self, offset):
            self.passadas += 1
            leva = self.levas.pop(0) if self.levas else []
            return [{"update_id": 1000 + self.passadas * 10 + i, "message": m}
                    for i, m in enumerate(leva)]

    def roda(levas):
        tg = TgRoteiro(levas)
        D.gravar_fila({"pendentes": []})
        D.gravar_state({"offset": 0, "avisos": {}, "erros": {}})
        os.environ.update({"TELEGRAM_TOKEN": "x", "TELEGRAM_CHAT_ID": "-1",
                           "GEMINI_API_KEY": "x"})
        original = sync.Telegram
        sync.Telegram = lambda *a, **k: tg
        try:
            sync.main()
        finally:
            sync.Telegram = original
        return tg

    tg = roda([[]])
    checa("sem mensagem nenhuma, encerra na primeira passada",
          tg.passadas == 1, f"{tg.passadas} passadas")

    tg = roda([[msg("ok", 1)], [msg("ok", 2)], []])
    checa("com movimento, continua checando",
          tg.passadas == 3, f"{tg.passadas} passadas")
    checa("encerra assim que o movimento para", tg.passadas == 3)

    # Fila travada que não anda: desiste depois de MAX_SEM_PROGRESSO e espera o cron.
    import erros
    import extrator
    original_ex = extrator.extrair

    def sempre_cai(api_key, **kw):
        raise erros.ErroTemporario("fora do ar", "🟡 fora", chave="gemini-instavel")

    extrator.extrair = sempre_cai
    tg = roda([[msg("ok", 1)], [], [], [], []])
    extrator.extrair = original_ex
    checa("fila travada não fica girando à toa",
          tg.passadas <= 1 + sync.MAX_SEM_PROGRESSO + 1, f"{tg.passadas} passadas")
    checa("e a mensagem continua guardada", len(D.ler_fila()["pendentes"]) == 1)

    sync.CICLO_RAPIDO = 180
    for k, v in guardado.items():
        if v is None:
            os.environ.pop(k, None)


def teste_arquivos_robustos():
    print("\n💾 Arquivos e escrita")
    base = os.path.dirname(D.LANCAMENTOS)

    with open(D.STATE, "w") as f:
        f.write("{isso não é json")
    st = D.ler_state()
    checa("JSON corrompido não derruba o run", st.get("offset") == 0)
    checa("o arquivo estragado é guardado pra perícia",
          os.path.exists(D.STATE + ".corrompido"))

    linhas = D.ler_lancamentos()
    with open(D.LANCAMENTOS, "a") as f:
        f.write("999,,2026-07-01,gasto,NÃO É NÚMERO,A,Outros,x,pix,,tg,alta,dito,,\n")
    checa("linha quebrada no CSV é pulada, não explode",
          len(D.ler_lancamentos()) == len(linhas))

    D.gravar_lancamentos(linhas)
    checa("escrita atômica não deixa .tmp pra trás",
          not any(f.endswith(".tmp") for f in os.listdir(base)))

    tg = FakeTelegram()
    from telegram import _fatiar
    pedacos = _fatiar("linha\n" * 3000, 4000)
    checa("mensagem gigante é fatiada pro limite do Telegram",
          all(len(p) <= 4000 for p in pedacos) and len(pedacos) > 1)


def teste_ifood():
    print("\n🍔 iFood: pedido x assinatura")
    from importar_historico import categoria, norm

    casos = [
        ("Ifd*Ifood Club", "iFood Club", "a assinatura de R$ 5,95 é custo fixo"),
        ("Ifd*Bento Lanches", "iFood", "pedido de comida é gasto variável"),
        ("Ifd*Daiane Santos Rosa", "iFood", "pedido com nome de pessoa"),
        ("Ifd*Mar e Sabor Restau", "iFood", "pedido de restaurante"),
    ]
    for titulo, esperado, nome in casos:
        obtido = categoria(norm(titulo))
        checa(f"{titulo} → {esperado} ({nome})", obtido == esperado, f"deu {obtido}")

    # O orçamento real do repositório, não o do sandbox.
    real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orcamento.json")
    with open(real, encoding="utf-8") as f:
        esp = D.esperado_por_categoria(json.load(f))
    checa("orçamento separa as duas linhas do iFood",
          esp.get("iFood Club") == 5.95 and "iFood" not in esp,
          f"iFood Club={esp.get('iFood Club')}, iFood={esp.get('iFood')}")


def teste_painel():
    print("\n📊 Painel")
    import painel
    caminho = painel.gerar(D.ler_lancamentos(), D.ler_orcamento(),
                           saida=os.path.join(os.path.dirname(D.LANCAMENTOS), "p.html"))
    html = open(caminho, encoding="utf-8").read()
    checa("gera HTML completo", html.startswith("<!doctype") and html.endswith("</html>"))
    checa("tem os dois blocos", "Conta A" in html and "Conta B" in html)
    checa("é autocontido (sem recurso externo)",
          "http://" not in html and "https://" not in html)
    checa("tem modo claro e escuro",
          "prefers-color-scheme" in html and 'data-theme="dark"' in html)


def main():
    tmp = sandbox()
    import extrator
    import sync
    extrator.extrair = fake_extrair
    sync.extrator = extrator

    def S():
        s = sync.Sessao(FakeTelegram(), "chave-falsa")
        return s

    try:
        teste_fatura()
        teste_roteamento(S)
        teste_pergunta_resolve(S)
        teste_ciclo_semanal(S)
        teste_fechamento(S)
        teste_isolamento(S)
        teste_intencoes(S)
        teste_idempotencia(S)
        teste_recorrente(S)
        teste_multi_lancamento(S)
        teste_ifood()
        teste_painel()
        teste_erros(S)
        teste_cadencia(S)
        teste_arquivos_robustos()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FALHAS:
        print(f"❌ {len(FALHAS)} falha(s): " + "; ".join(FALHAS))
        return 1
    print("✅ tudo passou")
    return 0


if __name__ == "__main__":
    sys.exit(main())
