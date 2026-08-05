"""Extrator: áudio ou texto → intenção estruturada, numa única chamada ao Gemini.

Gemini aceita áudio nativo, então não existe etapa separada de transcrição —
menos passo, menos erro acumulado. A saída é forçada por responseSchema, então
o modelo não consegue devolver formato torto.
"""

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request

import erros
from dados import CATEGORIAS, PADRAO_CONTA_B, brl, hoje

# O free tier do gemini-2.5-flash é de apenas 20 requisições POR DIA — inviável.
# O 3.5-flash-lite tem teto por minuto (15) em vez de teto diário apertado.
# A reserva entra quando o principal esgota, pra fila nunca ficar parada por cota.
MODELO = os.environ.get("GEMINI_MODELO", "gemini-3.5-flash-lite")
RESERVA = os.environ.get("GEMINI_MODELO_RESERVA", "gemini-3.6-flash")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def _config(modelo: str, schema: dict) -> dict:
    cfg = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseSchema": schema,
    }
    # thinkingBudget só existe na linha 2.x; nos modelos 3 ele derruba a requisição.
    if modelo.startswith("gemini-2"):
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    return cfg

INTENCOES = [
    "gasto", "receita", "consulta", "correcao", "exclusao",
    "orcamento", "recorrente", "fatura", "relatorio", "confirmacao", "fechar_mes", "nenhuma",
]

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcricao": {"type": "STRING"},
        "intencao": {"type": "STRING", "enum": INTENCOES},
        "precisa_perguntar": {"type": "BOOLEAN"},
        "pergunta": {"type": "STRING"},
        "resposta": {"type": "STRING"},
        "lancamentos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "valor": {"type": "NUMBER"},
                    "tipo": {"type": "STRING", "enum": ["gasto", "receita"]},
                    "conta": {"type": "STRING", "enum": ["A", "B"]},
                    "conta_origem": {"type": "STRING", "enum": ["dito", "padrao_categoria"]},
                    "categoria": {"type": "STRING", "enum": CATEGORIAS},
                    "descricao": {"type": "STRING"},
                    "data": {"type": "STRING"},
                    "pagamento": {
                        "type": "STRING",
                        "enum": ["credito", "debito", "pix", "dinheiro", "nao_informado"],
                    },
                    "confianca": {"type": "STRING", "enum": ["alta", "media", "baixa"]},
                },
                "required": ["valor", "tipo", "conta", "conta_origem", "categoria",
                             "descricao", "data", "pagamento", "confianca"],
            },
        },
        "alvo": {
            "type": "OBJECT",
            "properties": {
                "id": {"type": "STRING"},
                "campo": {"type": "STRING"},
                "valor_novo": {"type": "STRING"},
            },
            # `required` aqui não é enfeite. Sem ele o modelo omitia `campo`, e a
            # correção morria com "campo=None" mesmo tendo acertado o id — foi o que
            # aconteceu em 05/08 com "corrige o 26,92 do mercado, vai pra conta B".
            # Todos os outros objetos deste schema já declaravam required; estes dois
            # tinham sido esquecidos. O que não se aplica vem string vazia.
            "required": ["id", "campo", "valor_novo"],
        },
        "consulta": {
            "type": "OBJECT",
            "properties": {
                "periodo": {"type": "STRING", "enum": ["mes", "semana", "hoje", "tudo"]},
                "categoria": {"type": "STRING"},
                "conta": {"type": "STRING", "enum": ["A", "B", "ambas"]},
            },
            "required": ["periodo", "categoria", "conta"],
        },
    },
    "required": ["transcricao", "intencao", "precisa_perguntar", "lancamentos"],
}


PROMPT = """Você é o motor de um controle financeiro pessoal do Igor, brasileiro, de Dourados/MS.
Ele fala ou digita no Telegram e você transforma isso em ação estruturada. Responda SÓ o JSON do schema.

# Contexto de hoje
Data de hoje: {hoje} ({diasem}).
Fatura aberta: {fatura} (cartão fecha dia 15, vence dia 22).

# As duas carteiras — a parte mais importante
Conta A = dinheiro do Igor (salário R$ 1.600/mês). Conta B = dinheiro que o PAI dele manda (~R$ 350/semana).
Elas NUNCA se misturam. Errar a conta é o pior erro possível, porque contamina os dois orçamentos.

Como decidir, na ordem — a primeira regra que bater decide:
1. ELE DISSE. "conta B", "conta bê", "conta b", "no B", "é da B", "bê" → conta B, conta_origem="dito".
   "conta A", "conta a", "é minha", "do meu bolso", "pessoal" → conta A, conta_origem="dito".
   A palavra dita SEMPRE ganha, mesmo contrariando a categoria. É a palavra final dele.
2. PADRÃO POR CATEGORIA, se ele não disse nada: {padrao_b} → conta B. Todo o resto → conta A.
   conta_origem="padrao_categoria".
3. DESTOOU → PERGUNTE. Se cairia em B pela categoria mas o valor passa do limiar abaixo, NÃO grave:
   precisa_perguntar=true e pergunte qual conta. Limiares: {limiares}.
   Exemplo real: abastecer R$ 300 pra viajar é gasto DELE, não pode comer o dinheiro do mercado da semana.

Na dúvida entre A e B nunca chute calado: ou tem palavra dita, ou está dentro do padrão, ou pergunta.

# Intenções
- gasto: "gastei 35 no almoço", "35 no ifood", "paguei 110 da academia".
- receita: dinheiro ENTRANDO. "meu pai mandou 350" → tipo="receita", conta="B" (só isso abre semana
  nova). Amigo te pagou, salário, reembolso → tipo="receita", conta="A", categoria "Outros".
- consulta: "quanto gastei de comer fora?", "quanto sobrou da semana?" → preencha `consulta` e
  responda em `resposta` usando os dados abaixo. Não grave nada.
- correcao: ele está falando de um lançamento que JÁ EXISTE (veja a lista de recentes abaixo) e
  quer mudar alguma coisa nele. Preencha `alvo` com o **id** do lançamento, o `campo` e o
  `valor_novo`. Campos válidos: valor, categoria, conta, descricao, data.
  SEMPRE mande o `id` — é ele que faz a correção funcionar. Ache na lista de recentes pelo valor
  ("o 26,92"), pela descrição ("aquele mercado") ou por ser o último.
  Trocar a CONTA é correção, nunca um gasto novo. Se ele descreve algo que já está na lista e diz
  outra conta, é correcao — não lance de novo, senão o gasto entra em dobro. Exemplos, todos
  intencao=correcao com campo="conta":
    "o mercado das porcarias era pra ser na conta A"        → alvo={{id:<o id do mercado>, campo:"conta", valor_novo:"A"}}
    "corrigir o 26,92 mercado, ele deve ir pra conta A"     → alvo={{id:<id do 26,92>, campo:"conta", valor_novo:"A"}}
    "aquele posto de ontem não era do meu pai, era meu"     → alvo={{id:<id do posto>, campo:"conta", valor_novo:"A"}}
    "o uber foi na conta B, não na minha"                   → alvo={{id:<id do uber>, campo:"conta", valor_novo:"B"}}
  Outros campos: "aquele almoço foi 45, não 35" → campo="valor", valor_novo="45".
  "muda pra mercado" → campo="categoria", valor_novo="Mercado/Supermercado".
  Se você realmente não conseguir achar o id, ainda assim devolva intencao=correcao com o campo e o
  valor_novo preenchidos — o sistema tenta achar sozinho pelo valor citado.
  OBRIGATÓRIO: numa correcao, `campo` e `valor_novo` NUNCA podem vir vazios. Sem eles a correção
  é descartada, mesmo que o id esteja certo. `campo` é uma destas cinco palavras exatas:
  valor, categoria, conta, descricao, data.
- exclusao: "apaga o último" → `alvo.id`. Se estiver ambíguo qual é, pergunte. Aqui `campo` e
  `valor_novo` não se aplicam: devolva string vazia nos dois.
- orcamento: "meu esperado de comer fora é 300" → alvo.campo=categoria, alvo.valor_novo=valor.
- recorrente: "todo mês pago 110 de academia" → alvo com nome/valor.
- fatura: "quanto tá a fatura?", "quando fecha o cartão?" → responda em `resposta`.
- relatorio: "me manda o painel", "como tô esse mês?".
- fechar_mes: "fechou o mes", "virou o mes", "reinicia" → preencha intencao=fechar_mes.
- confirmacao: ele está respondendo uma pergunta sua que está aberta (veja pendências abaixo).
  Um "sim", "não", "foi 120", "é meu" isolado quase sempre é isso.
- nenhuma: mandou áudio/texto que não tem nada a ver com dinheiro. Fique CALADO, lista vazia,
  precisa_perguntar=false. Não force lançamento nunca.

# Vários lançamentos num áudio só — regra tão importante quanto a da conta
Quebre TUDO que foi dito em itens separados, um por gasto/receita. Nunca some valores diferentes
num item só, e nunca devolva só o primeiro que ouviu.
- "gastei 40 de mercado na conta B e 23 de restaurante" → 2 itens. Cada item tem a SUA conta: o
  mercado vai pra B (foi dito), o restaurante vai pra A (padrão). Um "conta B" dito no meio da
  frase vale só pro item a que ele se refere — não contamina os outros.
- "Igor, Kauan e Bruno me mandaram 5 reais ontem" → 3 receitas de R$ 5,00, uma por pessoa, com a
  data de ontem. Várias pessoas + um valor = aquele valor de CADA UMA. Só devolva um item único
  se ele disser que foi o total ("mandaram 5 no total", "juntaram 5 reais").
- "30 no almoço, 15 no Uber e 200 no mercado" → 3 itens, contas A, A e B.
- Itens repetidos são legítimos: "gastei 20 no Uber e mais 20 no Uber" → 2 itens iguais.
- Gasto e receita no mesmo áudio: use intencao="gasto" e marque o `tipo` certo em cada item.
- Se um item da lista precisar de pergunta e os outros não, devolva os que estão claros e
  pergunte só sobre o duvidoso.

# Números falados em português
"trinta e cinco" → 35.00 | "trinta e cinco e cinquenta" → 35.50 | "cento e dez" → 110.00
"duzentos e sessenta e um" → 261.00 | "mil e duzentos" → 1200.00 | "vinte e seis e noventa" → 26.90
"três e cinquenta" → 3.50 | "cem conto" → 100.00 | "duas pila" → 2.00
PROIBIDO INVENTAR VALOR. Sem número claro no áudio → precisa_perguntar=true e pergunte quanto foi.
Nunca chute um valor plausível.

# Recorrentes que ele já cadastrou
{recorrentes}
ÚNICA exceção à regra de nunca preencher valor: se ele citar um desses SEM falar o valor
("yt pago", "paguei o spotify", "academia paga", "pagei o clube do ifood"), use o valor
cadastrado acima, confianca="alta", e NÃO pergunte — ele já registrou esse número antes.
Se ele disser um valor diferente do cadastrado, o valor DITO ganha sempre.
Apelidos: "yt"/"youtube" = YouTube Premium; "academia"/"gym" = Iron Gym;
"clube do ifood"/"assinatura do ifood" = iFood Club.

# Datas
Sem data dita → hoje. "ontem", "anteontem", "sexta passada", "dia 3" → resolva a partir de hoje.
Sempre YYYY-MM-DD. Nunca uma data futura.

# Categorias e o vocabulário dele (vem das faturas reais)
{categorias}
Mercado/Supermercado: Leve Max, Pantanal, Melo e Cuenca, Machadão, Comper, Assaí, Ramin, "rancho".
Combustível: Auto Posto Guaicurus, Posto Benjamim, Ipiranga, Shell, Oshiro, "abasteci", "gasolina".
Marmita (Fitfood): Fitfood, "marmita". | Transporte/App: Uber, 99.
ATENÇÃO — os dois iFood são categorias DIFERENTES e não podem se misturar:
  · "iFood" = pedido de comida ("pedi ifood", "40 no ifood", "ifood de janta"). Gasto variável.
  · "iFood Club" = a assinatura mensal de ~R$ 5,95 ("clube do ifood", "assinatura do ifood",
    "mensalidade do ifood"). Custo fixo. Só use quando ele falar de clube/assinatura/mensalidade,
    ou quando for a cobrança recorrente de valor baixo. Pedido de comida NUNCA é iFood Club.
Saúde/Academia: Iron Gym, Strikers, farmácia, Droga Raia, Pague Menos, dentista.
Restaurantes/Lanches: almoço, janta, lanche, pizza, açaí, café, padaria, sushi, hambúrguer.
  Os daqui: Bentolanches, The Best Açaí, Padoca do Rei, Pão Dourado, Mycaohotdog, Dom Beer,
  Old Mexican, Vinis Pizzaria, Kukao Sorvetes, Espeto/Spetos, Cacau Show.
Streaming: Netflix, Spotify, YouTube, Disney, HBO, Prime. | Psicóloga: sessão, terapia, psicóloga.
Serviços online/Tech: Apple, Google, iCloud, Amazon, GitHub, Kabum. | Pix p/ pessoas: pix pra alguém.
Se nada encaixar: Outros. Categoria só PROVÁVEL não é motivo pra perguntar — chute a melhor e siga.

# Quando perguntar (e quando calar a boca)
PERGUNTE só se: falta o valor; o áudio está cortado/inaudível; a exclusão está ambígua; ou a conta
A vs B está genuinamente em aberto (regra 3). Uma pergunta curta, uma de cada vez.
NÃO PERGUNTE por categoria duvidosa, por forma de pagamento, nem por data — chute o mais provável.
Perguntar demais faz ele largar o sistema em duas semanas. Perguntar de menos faz o dado mentir.

# Pagamento
Só marque credito/debito/pix/dinheiro se ele disser. Caso contrário nao_informado.

# Exemplos
"gastei trinta e cinco reais no almoço hoje"
→ intencao=gasto, [{{valor:35.0, tipo:gasto, conta:A, conta_origem:padrao_categoria,
   categoria:"Restaurantes/Lanches", descricao:"almoço", data:{hoje}, pagamento:nao_informado, confianca:alta}}]

"duzentos no leve max"
→ conta B (mercado é padrão do pai, e 200 está dentro do limiar), confianca alta, sem pergunta.

"abasteci oitenta reais, conta B"
→ conta B com conta_origem="dito".

"abasteci trezentos pra viagem"
→ precisa_perguntar=true, pergunta="Combustível de R$ 300 — conta B ou é seu?", lancamentos=[].

"gastei uma grana no mercado ontem"
→ precisa_perguntar=true, pergunta="Quanto foi no mercado ontem?", lancamentos=[].

"meu pai mandou trezentos e cinquenta"
→ intencao=receita, [{{valor:350.0, tipo:receita, conta:B, categoria:"Outros", descricao:"ajuda do pai"}}]

"gastei quarenta de mercado na conta B e vinte e três de restaurante"
→ intencao=gasto, 2 itens:
   [{{valor:40.0, conta:B, conta_origem:dito, categoria:"Mercado/Supermercado", descricao:"mercado"}},
    {{valor:23.0, conta:A, conta_origem:padrao_categoria, categoria:"Restaurantes/Lanches",
      descricao:"restaurante"}}]

"Igor, Kauan e Bruno me mandaram cinco reais ontem"
→ intencao=receita, 3 itens de valor 5.0, tipo=receita, conta=A, categoria "Outros",
   descricao "Igor", "Kauan", "Bruno", data de ontem.

"quanto eu já gastei de comer fora esse mês?"
→ intencao=consulta, consulta={{periodo:mes, categoria:"Restaurantes/Lanches", conta:A}},
   resposta preenchida com o número real da lista abaixo. lancamentos=[].

"ó, depois eu te mando aquele arquivo que a gente tava vendo"
→ intencao=nenhuma, lancamentos=[], precisa_perguntar=false. Não responda nada.

# Estado atual (use pra responder consultas, corrigir e excluir)
Orçamento: {orcamento}
Últimos lançamentos: {recentes}
Semana da conta B: {semana}
Pendência aberta: {pendencia}
"""

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _contexto(orcamento, recentes, semana, pendencia, fatura, recorrentes):
    linhas = [
        f"id={l['id']} {l['data']} {brl(l['valor'])} [{l['conta']}] {l['categoria']} — {l['descricao']}"
        for l in recentes
    ]
    if semana:
        s = (f"acumulando desde {semana['ciclo']['inicio']}, "
             f"já gastou {brl(semana['gasto'])} — sem teto até ele dizer quanto o pai mandou")
    else:
        s = "período novo, nada gasto ainda"

    esperados = ", ".join(
        f"{i['categoria']}={i['esperado']}" for i in orcamento.get("A", {}).get("itens", [])
    )
    return {
        "hoje": hoje().isoformat(),
        "diasem": DIAS[hoje().weekday()],
        "fatura": fatura,
        "padrao_b": ", ".join(sorted(PADRAO_CONTA_B)),
        "limiares": ", ".join(
            f"{k} acima de R$ {v:.0f}"
            for k, v in orcamento.get("limiares", {}).items() if not k.startswith("_")
        ),
        "categorias": ", ".join(CATEGORIAS),
        "orcamento": f"renda A={orcamento.get('A', {}).get('renda')}, esperados: {esperados}; "
                     f"teto semanal B={orcamento.get('B', {}).get('teto_semanal')}",
        "recentes": "\n".join(linhas) or "(nenhum ainda)",
        "semana": s,
        "pendencia": pendencia or "nenhuma",
        "recorrentes": "\n".join(
            f"- {i['nome']}: R$ {float(i['valor']):.2f}, categoria \"{i['categoria']}\""
            for i in (recorrentes or {}).get("itens", [])
        ) or "(nenhum cadastrado)",
    }


def _post(corpo: dict, api_key: str, schema: dict, tentativas=3) -> dict:
    """Tenta o modelo principal; se a cota dele acabar, cai pro reserva."""
    modelos = [m for m in (MODELO, RESERVA) if m]
    ultimo = None
    for i, modelo in enumerate(modelos):
        try:
            return _post_modelo(corpo, api_key, schema, modelo, tentativas)
        except erros.ErroTemporario as e:
            ultimo = e
            # Só vale trocar de modelo quando o problema é cota — se a rede caiu
            # ou o serviço está fora, o reserva vai falhar igual.
            if e.chave not in ("gemini-cota-dia", "gemini-rpm") or i == len(modelos) - 1:
                raise
            print(f"{modelo} sem cota; tentando {modelos[i + 1]}")
    raise ultimo


def _post_modelo(corpo: dict, api_key: str, schema: dict, modelo: str, tentativas: int) -> dict:
    payload = json.dumps({
        "contents": [{"role": "user", "parts": corpo}],
        "generationConfig": _config(modelo, schema),
    }).encode()

    ultimo = None
    for tentativa in range(tentativas):
        req = urllib.request.Request(
            URL.format(modelo) + f"?key={api_key}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            return _extrair_texto(resp)
        except urllib.error.HTTPError as e:
            ultimo = erros.classificar_gemini(e.code, e.read().decode()[:800])
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            ultimo = erros.erro_rede("Gemini", str(e))
        except json.JSONDecodeError as e:
            ultimo = erros.erro_rede("Gemini", f"JSON inválido: {e}")
        except erros.ErroSistema as e:
            ultimo = e

        if isinstance(ultimo, erros.ErroPermanente) or tentativa == tentativas - 1:
            break
        # Cota diária só volta amanhã: insistir aqui é queimar minuto do Actions à toa.
        if ultimo.chave == "gemini-cota-dia":
            break
        # Limite por minuto passa sozinho em segundos — vale esperar de verdade.
        time.sleep(ultimo.esperar if ultimo.chave == "gemini-rpm" else 2 ** tentativa)
    raise ultimo


def _extrair_texto(resp: dict) -> dict:
    candidatos = resp.get("candidates") or []
    if not candidatos:
        motivo = (resp.get("promptFeedback") or {}).get("blockReason", "sem candidatos")
        raise erros.erro_sem_resposta(str(motivo))

    c = candidatos[0]
    partes = (c.get("content") or {}).get("parts") or []
    if not partes or "text" not in partes[0]:
        raise erros.erro_sem_resposta(str(c.get("finishReason", "resposta vazia")))

    try:
        return json.loads(partes[0]["text"])
    except json.JSONDecodeError as e:
        raise erros.ErroTemporario(
            f"Gemini devolveu JSON quebrado: {e}",
            "🟡 O Gemini devolveu uma resposta malformada. Tento de novo no próximo ciclo.",
            chave="gemini-json",
        )


def extrair(api_key, *, audio=None, mime="audio/ogg", texto=None,
            orcamento=None, recentes=(), semana=None, pendencia=None, fatura="",
            recorrentes=None) -> dict:
    """Manda áudio ou texto pro Gemini e devolve a intenção estruturada."""
    prompt = PROMPT.format(**_contexto(orcamento or {}, list(recentes), semana,
                                       pendencia, fatura, recorrentes))

    partes = [{"text": prompt}]
    if audio:
        partes.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}})
        partes.append({"text": "Processe o áudio acima."})
    else:
        partes.append({"text": f"Mensagem de texto do Igor: {texto!r}"})

    out = _post(partes, api_key, SCHEMA)
    out.setdefault("lancamentos", [])
    out.setdefault("precisa_perguntar", False)
    out.setdefault("intencao", "nenhuma")
    if not audio and texto and not out.get("transcricao"):
        out["transcricao"] = texto
    return out


REVISAO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sugestoes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "categoria": {"type": "STRING"},
                    "esperado_atual": {"type": "NUMBER"},
                    "esperado_sugerido": {"type": "NUMBER"},
                    "motivo": {"type": "STRING"},
                },
                "required": ["categoria", "esperado_atual", "esperado_sugerido", "motivo"],
            },
        },
        "resumo": {"type": "STRING"},
    },
    "required": ["sugestoes", "resumo"],
}

PROMPT_REVISAO = """Você revisa o orçamento mensal do Igor. Abaixo, o esperado que ele definiu e o
gasto real dos últimos meses por categoria (só conta A).

Esperado hoje: {esperado}
Real por mês: {historico}

Proponha ajuste APENAS onde a diferença for consistente (3+ meses na mesma direção), não onde teve
um pico isolado. Aponte também assinatura que ele espera pagar mas não aparece cobrança há 2+ meses
— pode ser assinatura fantasma, e é onde mora dinheiro fácil.
Seja direto e curto. Você só PROPÕE; ele aprova por voz. Máximo 4 sugestões.
"""


def revisar_orcamento(api_key, esperado: dict, historico: dict) -> dict:
    corpo = [{"text": PROMPT_REVISAO.format(
        esperado=json.dumps(esperado, ensure_ascii=False),
        historico=json.dumps(historico, ensure_ascii=False),
    )}]
    return _post(corpo, api_key, REVISAO_SCHEMA, tentativas=1)
