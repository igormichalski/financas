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


def _caminhos_reais(mod):
    return [n for n in dir(mod)
            if n.isupper() and isinstance(getattr(mod, n), str)
            and getattr(mod, n).startswith(D.BASE + os.sep)]


def sandbox():
    """Redireciona TODO arquivo de dados pra uma pasta temporária.

    Descobre os caminhos sozinho em vez de manter uma lista à mão: uma lista
    esquecida já fez um teste sobrescrever a fila real de mensagens.

    Varre `painel` junto com `dados`: varrer só o `dados` deixava o painel.SAIDA
    apontando pro repositório, e rodar a suíte reescrevia o painel.html de verdade
    com os lançamentos de mentira dos testes.
    """
    import painel

    tmp = tempfile.mkdtemp(prefix="financas-teste-")
    modulos = (D, painel)
    for mod in modulos:
        for nome in _caminhos_reais(mod):
            setattr(mod, nome, os.path.join(tmp, os.path.basename(getattr(mod, nome))))
    os.makedirs(D.INBOX, exist_ok=True)

    # Trava: se sobrar qualquer caminho apontando pro repositório, aborta antes
    # de escrever em cima dos dados de verdade.
    vazando = {mod.__name__: _caminhos_reais(mod) for mod in modulos}
    vazando = {k: v for k, v in vazando.items() if v}
    if vazando:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"ABORTADO: {vazando} ainda apontam pro repositório real")

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
    print("\n👨 Conta do pai: acumula, depois fecha com o que ele mandou")
    import sync

    D.gravar_semanas({"ciclos": [], "acumulado": 0.0})
    RESPOSTAS.clear()
    RESPOSTAS.update({
        "mercado244": {"transcricao": "244,32 no mercado", "intencao": "gasto",
                       "precisa_perguntar": False,
                       "lancamentos": [lanc(244.32, "Mercado/Supermercado", descricao="mercado")]},
        "pai250": {"transcricao": "meu pai mandou 250", "intencao": "receita",
                   "precisa_perguntar": False,
                   "lancamentos": [lanc(250, "Outros", "B", "dito", "receita",
                                        descricao="ajuda do pai")]},
        "gasta100": {"transcricao": "100 no mercado", "intencao": "gasto",
                     "precisa_perguntar": False,
                     "lancamentos": [lanc(100, "Mercado/Supermercado", descricao="depois")]},
        "pai90": {"transcricao": "meu pai mandou 90", "intencao": "receita",
                  "precisa_perguntar": False,
                  "lancamentos": [lanc(90, "Outros", "B", "dito", "receita",
                                       descricao="ajuda do pai")]},
    })

    s = S()
    s.processar(msg("mercado244", 1))
    sem = sync.contexto_semana(s.linhas)
    checa("gasto acumula sem teto nenhum",
          sem and sem["gasto"] == 244.32 and "teto" not in sem["ciclo"],
          f"{sem}")

    s.processar(msg("pai250", 2))
    fechados = [c for c in D.ler_semanas()["ciclos"] if c.get("fechado_em")]
    checa("dizer o valor fecha o período com o que JÁ foi gasto",
          len(fechados) == 1 and fechados[0]["gasto"] == 244.32
          and fechados[0]["recebido"] == 250.0,
          str(fechados))
    checa("sobra = recebido − gasto = 5,68",
          abs(fechados[0]["sobra"] - 5.68) < 0.01, f"deu {fechados[0]['sobra']}")
    checa("a sobra vai pro guardado", abs(D.acumulado() - 5.68) < 0.01, f"{D.acumulado()}")
    checa("período novo começa zerado",
          sync.contexto_semana(s.linhas)["gasto"] == 0.0)
    checa("o bot mostra a conta do fechamento",
          any("PERÍODO FECHADO" in r and "sobrou" in r for r in s.respostas),
          str(s.respostas[-1:]))

    # O ciclo do mês vira dia 10, então o rótulo de hoje NÃO é hoje[:7].
    mes = D.mes_aberto_a()
    resumo = sync.montar_resumo(s.linhas, s.orcamento)
    checa("gasto da conta B não entra no total do salário",
          D.gastos_do_mes(s.linhas, mes, "A") == 0.0
          and D.gastos_do_mes(s.linhas, mes, "B") == 244.32)
    checa("o resumo mostra os dois blocos separados",
          "SEU DINHEIRO" in resumo and "CONTA DO PAI" in resumo)

    s.processar(msg("gasta100", 3))
    checa("gasto depois da virada entra só no período novo",
          sync.contexto_semana(s.linhas)["gasto"] == 100.0)

    s.processar(msg("pai90", 4))
    fechados = [c for c in D.ler_semanas()["ciclos"] if c.get("fechado_em")]
    checa("recebendo menos que o gasto, a sobra fica negativa",
          fechados[1]["sobra"] == -10.0, f"deu {fechados[1]['sobra']}")
    checa("e o guardado absorve a diferença",
          abs(D.acumulado() - (5.68 - 10.0)) < 0.01, f"{D.acumulado()}")
    checa("período fechado não muda mais com lançamento novo",
          D.gasto_no_ciclo(s.linhas, fechados[0]) == 244.32)


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
    # O ciclo do mês vira dia 10, então o rótulo de hoje NÃO é hoje[:7].
    mes = D.mes_aberto_a()
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


def teste_correcao(S):
    """Regressão do pior bug encontrado: em 04/08/2026 o Igor pediu DUAS vezes pra
    mover um gasto de mercado da conta B pra A, as duas mensagens foram processadas,
    e o ledger não mudou nem sobrou rastro em lugar nenhum."""
    print("\n✏️  Correção de lançamento")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "mercado": {"transcricao": "26,92 mercado porcarias para cinema", "intencao": "gasto",
                    "precisa_perguntar": False,
                    "lancamentos": [lanc(26.92, "Mercado/Supermercado",
                                         descricao="mercado porcarias para cinema")]},
        # O Gemini erra o id com frequência. Antes, id errado = correção sumia calada.
        "conta_sem_id": {"transcricao": "Corrigir o 26,92 mercado porcarias, vai pra conta A",
                         "intencao": "correcao", "precisa_perguntar": False, "lancamentos": [],
                         "alvo": {"id": "", "campo": "conta", "valor_novo": "A"}},
        "conta_id_errado": {"transcricao": "o mercado porcarias era pra ser na conta A",
                            "intencao": "correcao", "precisa_perguntar": False, "lancamentos": [],
                            "alvo": {"id": "999", "campo": "conta", "valor_novo": "A"}},
        "impossivel": {"transcricao": "corrige aquilo lá", "intencao": "correcao",
                       "precisa_perguntar": False, "lancamentos": [],
                       "alvo": {"id": "", "campo": "", "valor_novo": ""}},
    })

    s = S()
    s.processar(msg("mercado", 1))
    checa("o mercado entrou na conta B pelo padrão da categoria",
          s.linhas[-1]["conta"] == "B", s.linhas[-1]["conta"])

    s.processar(msg("conta_sem_id", 2))
    checa("sem id, acha o lançamento pelo valor citado e move pra conta A",
          s.linhas[-1]["conta"] == "A", s.linhas[-1]["conta"])
    checa("mover de conta marca a origem como dita",
          s.linhas[-1]["conta_origem"] == "dito")
    checa("a correção não vira lançamento novo", len(s.linhas) == 1, f"{len(s.linhas)} linhas")
    checa("o bot diz qual lançamento mexeu",
          any("#1" in r for r in s.respostas), str(s.respostas[-2:]))

    s2 = S()
    s2.processar(msg("mercado", 10))
    s2.processar(msg("conta_id_errado", 11))
    checa("id errado cai no casamento por descrição",
          s2.linhas[-1]["conta"] == "A", s2.linhas[-1]["conta"])

    for bruto, esperado in [("26,92", 26.92), ("26.92", 26.92), ("1.234,56", 1234.56),
                            ("45", 45.0), ("abc", None), ("", None)]:
        checa(f"valor à brasileira: {bruto!r} → {esperado}",
              sync._valor_br(bruto) == esperado, str(sync._valor_br(bruto)))

    # "foi 45, não 35": o 45 ainda não existe no ledger, o alvo é o 35.
    RESPOSTAS["dois_numeros"] = {
        "transcricao": "aquele mercado foi 45, não 26,92", "intencao": "correcao",
        "precisa_perguntar": False, "lancamentos": [],
        "alvo": {"id": "", "campo": "valor", "valor_novo": "45"}}
    s4 = S()
    s4.processar(msg("mercado", 30))
    s4.processar(msg("dois_numeros", 31))
    checa("com dois números na frase, corrige o valor certo",
          s4.linhas[-1]["valor"] == 45.0, str(s4.linhas[-1]["valor"]))

    # Correção que não dá pra resolver não pode evaporar: tem que sobrar rastro.
    s3 = S()
    s3.processar(msg("mercado", 20))
    s3.processar(msg("impossivel", 21))
    with open(D.REVISAR, encoding="utf-8") as f:
        revisar = f.read()
    checa("correção sem alvo vai pro revisar.csv em vez de sumir",
          "correção não aplicada" in revisar, revisar[-160:])
    checa("e o bot avisa que não conseguiu",
          any("Não achei qual lançamento" in r for r in s3.respostas))


def teste_schema_gemini(S):
    """O responseSchema é o único contrato que impede o Gemini de devolver meia resposta.

    Objeto sem `required` deixa o modelo omitir campo à vontade. Aconteceu de verdade em
    05/08: "corrige o 26,92 do mercado, vai pra conta B" voltou com o id certo e
    campo=None, e a correção foi descartada. Os outros objetos do schema já declaravam
    required; alvo e consulta tinham sido esquecidos.
    """
    print("\n📐 Contrato do schema do Gemini")
    import extrator

    props = extrator.SCHEMA["properties"]

    for nome, campos in [("alvo", ["id", "campo", "valor_novo"]),
                         ("consulta", ["periodo", "categoria", "conta"])]:
        obj = props[nome]
        checa(f"{nome} declara required", "required" in obj, str(list(obj)))
        faltando = [c for c in campos if c not in obj.get("required", [])]
        checa(f"{nome} exige {', '.join(campos)}", not faltando, f"faltam {faltando}")

    # Todo objeto do schema tem que declarar required, senão o mesmo bug volta noutro campo.
    def varre(no, caminho="raiz"):
        if not isinstance(no, dict):
            return []
        ruins = []
        if no.get("type") == "OBJECT" and "required" not in no:
            ruins.append(caminho)
        for k, v in (no.get("properties") or {}).items():
            ruins += varre(v, f"{caminho}.{k}")
        if no.get("type") == "ARRAY":
            ruins += varre(no.get("items") or {}, f"{caminho}[]")
        return ruins

    orfaos = varre(extrator.SCHEMA)
    checa("nenhum objeto do schema sem required", not orfaos, str(orfaos))

    # E o campo que a correção aceita tem que casar com o que o código sabe aplicar.
    import sync
    validos = set(sync.Sessao.CAMPOS_CORRIGIVEIS)
    checa("prompt cita exatamente os campos corrigíveis",
          all(c in extrator.PROMPT for c in validos), str(validos))


def teste_inbox(S):
    """O webhook do Cloudflare larga cada mensagem em inbox/*.json."""
    print("\n📥 Caixa de entrada do webhook")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS["oi"] = {"transcricao": "oi", "intencao": "nenhuma",
                       "precisa_perguntar": False, "lancamentos": []}

    def escrever(update_id, payload):
        os.makedirs(D.INBOX, exist_ok=True)
        with open(os.path.join(D.INBOX, f"{update_id}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    tg = FakeTelegram()
    state = {"offset": 0}
    escrever(5000, {"update_id": 5000, "message": msg("oi", 1)})
    fila, novos = sync.coletar(tg, state)
    checa("mensagem da inbox entra na fila", novos == 1, str(novos))
    checa("o offset anda com o update_id da inbox", state["offset"] == 5001, str(state["offset"]))
    checa("o arquivo sai da inbox depois de lido",
          not os.listdir(D.INBOX))

    # O offset nunca pode retroceder: se voltar, o Telegram re-entrega tudo e uma
    # correção ou um "meu pai mandou 350" seria reprocessado.
    escrever(4000, {"update_id": 4000, "message": msg("oi", 2)})
    sync.coletar(tg, state)
    checa("update antigo não puxa o offset pra trás",
          state["offset"] == 5001, str(state["offset"]))

    # Mensagem de outra conversa não pode entrar no ledger.
    escrever(6000, {"update_id": 6000,
                    "message": {"message_id": 9, "text": "oi",
                                "chat": {"id": "outro"}, "from": {"is_bot": False}}})
    fila, novos = sync.coletar(tg, state)
    checa("mensagem de outro chat é descartada", novos == 0, str(novos))
    checa("mas o offset avança mesmo assim", state["offset"] == 6001, str(state["offset"]))

    # Arquivo quebrado ficava girando pra sempre, retentado em todo run.
    os.makedirs(D.INBOX, exist_ok=True)
    with open(os.path.join(D.INBOX, "7000.json"), "w", encoding="utf-8") as f:
        f.write("{isso não é json")
    sync.coletar(tg, state)
    checa("arquivo ilegível sai da inbox em vez de travar todo run",
          not os.listdir(D.INBOX), str(os.listdir(D.INBOX)))
    with open(D.REVISAR, encoding="utf-8") as f:
        checa("e fica registrado pra perícia", "inbox ilegível" in f.read())


def teste_ciclo_mensal(S):
    """O ciclo da conta A vira dia 10, e essa régua tem que valer no arquivo inteiro."""
    print("\n🗓  Ciclo mensal da conta A (vira dia 10)")

    casos = [
        ("2026-08-09", "2026-07", "véspera da virada ainda é o ciclo anterior"),
        ("2026-08-10", "2026-08", "no dia 10 o ciclo novo abre"),
        ("2026-08-31", "2026-08", "fim do mês segue no ciclo do mês"),
        ("2026-01-05", "2025-12", "virada de ano pra trás"),
        ("2026-01-10", "2026-01", "e janeiro abre no dia 10"),
    ]
    for d, esperado, nome in casos:
        obtido = D.ciclo_de(d)
        checa(f"{d} → {esperado} ({nome})", obtido == esperado, f"deu {obtido}")

    # A regra do fallback tem que ser a MESMA do novo_lancamento, senão linha editada
    # à mão e linha nova do mesmo dia caem em ciclos diferentes.
    D.gravar_lancamentos([])
    with open(D.LANCAMENTOS, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(D.CAMPOS) + "\n")
        f.write("1,,2026-08-05,gasto,10.0,A,Outros,mao,nao_informado,,manual,alta,dito,,,\n")
    lidas = D.ler_lancamentos()
    checa("linha sem mes_ref usa a mesma régua do ciclo",
          lidas[0]["mes_ref"] == "2026-07", lidas[0]["mes_ref"])
    checa("e mes_de concorda com ela", D.mes_de(lidas[0]) == D.ciclo_de("2026-08-05"))

    checa("proximo_mes vira o ano", D.proximo_mes("2026-12") == "2027-01")
    checa("mes_anterior vira o ano", D.mes_anterior("2026-01") == "2025-12")


def teste_entradas_restantes(S):
    """Fecha a cobertura das 12 intenções que o extrator declara.

    Faltavam `recorrente`, `relatorio` e `fechar_mes` — e o `fechar_mes` é exatamente o
    tipo de coisa que apodrece calada: ele virou informativo quando o ciclo passou a
    virar sozinho, e nada garantia que ainda respondesse algo coerente.
    """
    print("\n🎛  Entradas que faltavam")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS.update({
        "cadastra_rec": {
            "transcricao": "todo mês pago 79,90 de internet", "intencao": "recorrente",
            "precisa_perguntar": False,
            "lancamentos": [lanc(79.90, "Serviços online/Tech", descricao="internet")],
            "alvo": {"campo": "Internet", "valor_novo": "79.90"}},
        "quer_painel": {"transcricao": "me manda o painel", "intencao": "relatorio",
                        "precisa_perguntar": False, "lancamentos": []},
        "virou": {"transcricao": "fechou o mes", "intencao": "fechar_mes",
                  "precisa_perguntar": False, "lancamentos": []},
        "foto": {"transcricao": "42 no cinema", "intencao": "gasto",
                 "precisa_perguntar": False,
                 "lancamentos": [lanc(42, "Restaurantes/Lanches", descricao="cinema")]},
    })

    s = S()

    # ---- recorrente: cadastra, mas NUNCA lança sozinho
    antes = len(s.linhas)
    s.processar(msg("cadastra_rec", 1))
    itens = {i["nome"]: i for i in D.ler_recorrentes()["itens"]}
    checa("recorrente novo é cadastrado", "Internet" in itens, str(list(itens)))
    checa("com o valor certo", itens.get("Internet", {}).get("valor") == 79.90,
          str(itens.get("Internet")))
    checa("cadastrar recorrente NÃO lança gasto", len(s.linhas) == antes,
          f"{len(s.linhas)} vs {antes}")
    checa("e o bot avisa que não vai lançar sozinho",
          any("Não vou lançar sozinho" in r for r in s.respostas), str(s.respostas[-1:]))

    # ---- relatorio: sinaliza o painel sem mexer no ledger
    s2 = S()
    s2.processar(msg("quer_painel", 2))
    checa("pedir painel sinaliza __RELATORIO__", "__RELATORIO__" in s2.respostas,
          str(s2.respostas))
    checa("pedir painel não altera o ledger", not s2.mudou)

    # ---- fechar_mes: virou informativo, mas tem que responder algo coerente
    s3 = S()
    s3.processar(msg("virou", 3))
    resp = " ".join(s3.respostas)
    checa("fechar_mes responde explicando a virada automática",
          str(D.DIA_VIRADA) in resp and "vira sozinho" in resp, resp[:120])
    checa("fechar_mes não inventa mês nem mexe em nada", not s3.mudou)
    mes = D.mes_aberto_a()
    checa("e cita o ciclo aberto de verdade",
          MESES_PT[int(mes[5:7]) - 1] in resp, f"{resp[:120]} (esperava {mes})")

    # ---- foto com legenda: o gasto está na caption, não em text
    s4 = S()
    foto = {"message_id": 4, "caption": "foto", "chat": {"id": "-1"},
            "from": {"is_bot": False},
            "photo": [{"file_id": "abc", "file_size": 100}]}
    s4.processar(foto)
    checa("gasto na legenda de uma foto é lançado",
          any(l["valor"] == 42.0 for l in s4.novos), str([l["valor"] for l in s4.novos]))

    # ---- mensagem sem texto e sem mídia: não pode explodir nem responder
    s5 = S()
    s5.processar({"message_id": 5, "chat": {"id": "-1"}, "from": {"is_bot": False}})
    checa("mensagem vazia é ignorada em silêncio",
          not s5.novos and not s5.respostas and not s5.mudou)


MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


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

    # O ciclo do mês vira dia 10, então o rótulo de hoje NÃO é hoje[:7].
    mes = D.mes_aberto_a()
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


def teste_silencio(S):
    print("\n🤫 Só fala quando tem novidade")
    import sync

    RESPOSTAS.clear()
    RESPOSTAS["gasto"] = {"transcricao": "35 no almoço", "intencao": "gasto",
                          "precisa_perguntar": False,
                          "lancamentos": [lanc(35, "Restaurantes/Lanches", descricao="almoço")]}

    class TgConta(FakeTelegram):
        def updates(self, offset):
            leva = self.levas.pop(0) if getattr(self, "levas", None) else []
            return [{"update_id": 5000 + offset + i, "message": m} for i, m in enumerate(leva)]

    def roda(levas, relatorio):
        tg = TgConta()
        tg.levas = list(levas)
        os.environ.update({"TELEGRAM_TOKEN": "x", "TELEGRAM_CHAT_ID": "-1",
                           "GEMINI_API_KEY": "x"})
        os.environ["RELATORIO"] = "1" if relatorio else "0"
        original = sync.Telegram
        sync.Telegram = lambda *a, **k: tg
        try:
            sync.main()
        finally:
            sync.Telegram = original
            os.environ.pop("RELATORIO", None)
        return tg

    D.gravar_fila({"pendentes": []})
    D.gravar_state({"offset": 0, "avisos": {}, "erros": {}})
    D.gravar_lancamentos([])
    D.gravar_pendencias({"abertas": []})
    D.gravar_recorrentes({"itens": []})
    sync.CICLO_RAPIDO = 0

    tg = roda([[msg("gasto", 1)]], relatorio=False)
    checa("custo mudou → manda o resumo",
          len(tg.enviadas) >= 1, f"{len(tg.enviadas)} msg")
    checa("mas não manda o painel sem pedir", not tg.docs, f"{len(tg.docs)} doc à toa")

    tg = roda([[]], relatorio=False)
    checa("sem alteração de custo: silêncio total",
          not tg.enviadas and not tg.docs,
          f"mandou {len(tg.enviadas)} msg e {len(tg.docs)} doc à toa")

    tg = roda([[msg("gasto", 2)]], relatorio=False)
    checa("novo gasto → volta a falar", len(tg.enviadas) >= 1)

    tg = roda([[]], relatorio=True)
    checa("pedindo o painel explicitamente, ele vem mesmo sem novidade",
          len(tg.docs) == 1, f"{len(tg.docs)} doc")

    sync.CICLO_RAPIDO = 180


def teste_cadencia_padrao(S):
    """Regressão de latência. Com o webhook, cada mensagem dispara o seu próprio run, e
    o workflow tem `concurrency: group: sync`. Um run que fica vivo depois de terminar
    SEGURA A FILA da mensagem seguinte — medido em 05/08: "Painel" às 13:01:58 só saiu
    às 13:05 porque o run anterior dormia 180s à toa. O padrão tem que ser passada única."""
    print("\n⚡ Cadência padrão: uma passada e morre")
    import importlib
    import sync

    guardado = os.environ.pop("CICLO_RAPIDO", None)
    try:
        importlib.reload(sync)
        checa("CICLO_RAPIDO padrão é 0 (sem loop)", sync.CICLO_RAPIDO == 0,
              f"deu {sync.CICLO_RAPIDO}")
    finally:
        if guardado is not None:
            os.environ["CICLO_RAPIDO"] = guardado
        importlib.reload(sync)

    # E dá pra religar o loop por ambiente, pra quando a fila estiver travada.
    os.environ["CICLO_RAPIDO"] = "7"
    try:
        importlib.reload(sync)
        checa("mas segue configurável por ambiente", sync.CICLO_RAPIDO == 7,
              f"deu {sync.CICLO_RAPIDO}")
    finally:
        os.environ.pop("CICLO_RAPIDO", None)
        importlib.reload(sync)


def teste_cadencia(S):
    print("\n⏱  Cadência adaptativa (quando religada por ambiente)")
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
        teste_isolamento(S)
        teste_intencoes(S)
        teste_correcao(S)
        teste_schema_gemini(S)
        teste_inbox(S)
        teste_ciclo_mensal(S)
        teste_entradas_restantes(S)
        teste_idempotencia(S)
        teste_recorrente(S)
        teste_multi_lancamento(S)
        teste_ifood()
        teste_painel()
        teste_erros(S)
        teste_silencio(S)
        teste_cadencia_padrao(S)
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
