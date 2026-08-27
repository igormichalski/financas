import base64
import json
import os
import socket
import time
import urllib.error
import urllib.request
import erros
from dados import CATEGORIAS, PADRAO_CONTA_B, brl, hoje
MODELO = os.environ.get('GEMINI_MODELO', 'gemini-3.5-flash-lite')
RESERVA = os.environ.get('GEMINI_MODELO_RESERVA', 'gemini-3.6-flash')
URL = 'https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'

def _config(modelo: str, schema: dict) -> dict:
    cfg = {'temperature': 0, 'responseMimeType': 'application/json', 'responseSchema': schema}
    if modelo.startswith('gemini-2'):
        cfg['thinkingConfig'] = {'thinkingBudget': 0}
    return cfg
INTENCOES = ['gasto', 'receita', 'consulta', 'correcao', 'exclusao', 'orcamento', 'recorrente', 'fatura', 'relatorio', 'confirmacao', 'fechar_mes', 'nenhuma']
SCHEMA = {'type': 'OBJECT', 'properties': {'transcricao': {'type': 'STRING'}, 'intencao': {'type': 'STRING', 'enum': INTENCOES}, 'precisa_perguntar': {'type': 'BOOLEAN'}, 'pergunta': {'type': 'STRING'}, 'resposta': {'type': 'STRING'}, 'lancamentos': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {'valor': {'type': 'NUMBER'}, 'tipo': {'type': 'STRING', 'enum': ['gasto', 'receita']}, 'conta': {'type': 'STRING', 'enum': ['A', 'B']}, 'conta_origem': {'type': 'STRING', 'enum': ['dito', 'padrao_categoria']}, 'categoria': {'type': 'STRING', 'enum': CATEGORIAS}, 'descricao': {'type': 'STRING'}, 'data': {'type': 'STRING'}, 'pagamento': {'type': 'STRING', 'enum': ['credito', 'debito', 'pix', 'dinheiro', 'nao_informado']}, 'confianca': {'type': 'STRING', 'enum': ['alta', 'media', 'baixa']}}, 'required': ['valor', 'tipo', 'conta', 'conta_origem', 'categoria', 'descricao', 'data', 'pagamento', 'confianca']}}, 'alvo': {'type': 'OBJECT', 'properties': {'id': {'type': 'STRING'}, 'campo': {'type': 'STRING'}, 'valor_novo': {'type': 'STRING'}}, 'required': ['id', 'campo', 'valor_novo']}, 'consulta': {'type': 'OBJECT', 'properties': {'periodo': {'type': 'STRING', 'enum': ['mes', 'semana', 'hoje', 'tudo']}, 'categoria': {'type': 'STRING'}, 'conta': {'type': 'STRING', 'enum': ['A', 'B', 'ambas']}}, 'required': ['periodo', 'categoria', 'conta']}}, 'required': ['transcricao', 'intencao', 'precisa_perguntar', 'lancamentos']}
PROMPT = 'Você é o motor de um controle financeiro pessoal do Igor, brasileiro, de Dourados/MS.\nEle fala ou digita no Telegram e você transforma isso em ação estruturada. Responda SÓ o JSON do schema.\n\n# Contexto de hoje\nData de hoje: {hoje} ({diasem}).\nFatura aberta: {fatura} (cartão fecha dia 15, vence dia 22).\n\n# As duas carteiras — a parte mais importante\nConta A = dinheiro do Igor (salário R$ 1.600/mês). Conta B = dinheiro que o PAI dele manda (~R$ 350/semana).\nElas NUNCA se misturam. Errar a conta é o pior erro possível, porque contamina os dois orçamentos.\n\nComo decidir, na ordem — a primeira regra que bater decide:\n1. ELE DISSE. "conta B", "conta bê", "conta b", "no B", "é da B", "bê" → conta B, conta_origem="dito".\n   "conta A", "conta a", "é minha", "do meu bolso", "pessoal" → conta A, conta_origem="dito".\n   A palavra dita SEMPRE ganha, mesmo contrariando a categoria. É a palavra final dele.\n2. PADRÃO POR CATEGORIA, se ele não disse nada: {padrao_b} → conta B. Todo o resto → conta A.\n   conta_origem="padrao_categoria".\n3. DESTOOU → PERGUNTE. Se cairia em B pela categoria mas o valor passa do limiar abaixo, NÃO grave:\n   precisa_perguntar=true e pergunte qual conta. Limiares: {limiares}.\n   Exemplo real: abastecer R$ 300 pra viajar é gasto DELE, não pode comer o dinheiro do mercado da semana.\n\nNa dúvida entre A e B nunca chute calado: ou tem palavra dita, ou está dentro do padrão, ou pergunta.\n\n# Intenções\n- gasto: "gastei 35 no almoço", "35 no ifood", "paguei 110 da academia".\n- receita: dinheiro ENTRANDO. "meu pai mandou 350" → tipo="receita", conta="B" (só isso abre semana\n  nova). Amigo te pagou, salário, reembolso → tipo="receita", conta="A", categoria "Outros".\n- consulta: "quanto gastei de comer fora?", "quanto sobrou da semana?" → preencha `consulta` e\n  responda em `resposta` usando os dados abaixo. Não grave nada.\n- correcao: ele está falando de um lançamento que JÁ EXISTE (veja a lista de recentes abaixo) e\n  quer mudar alguma coisa nele. Preencha `alvo` com o **id** do lançamento, o `campo` e o\n  `valor_novo`. Campos válidos: valor, categoria, conta, descricao, data.\n  SEMPRE mande o `id` — é ele que faz a correção funcionar. Ache na lista de recentes pelo valor\n  ("o 26,92"), pela descrição ("aquele mercado") ou por ser o último.\n  Trocar a CONTA é correção, nunca um gasto novo. Se ele descreve algo que já está na lista e diz\n  outra conta, é correcao — não lance de novo, senão o gasto entra em dobro. Exemplos, todos\n  intencao=correcao com campo="conta":\n    "o mercado das porcarias era pra ser na conta A"        → alvo={{id:<o id do mercado>, campo:"conta", valor_novo:"A"}}\n    "corrigir o 26,92 mercado, ele deve ir pra conta A"     → alvo={{id:<id do 26,92>, campo:"conta", valor_novo:"A"}}\n    "aquele posto de ontem não era do meu pai, era meu"     → alvo={{id:<id do posto>, campo:"conta", valor_novo:"A"}}\n    "o uber foi na conta B, não na minha"                   → alvo={{id:<id do uber>, campo:"conta", valor_novo:"B"}}\n  Outros campos: "aquele almoço foi 45, não 35" → campo="valor", valor_novo="45".\n  "muda pra mercado" → campo="categoria", valor_novo="Mercado/Supermercado".\n  Se você realmente não conseguir achar o id, ainda assim devolva intencao=correcao com o campo e o\n  valor_novo preenchidos — o sistema tenta achar sozinho pelo valor citado.\n  OBRIGATÓRIO: numa correcao, `campo` e `valor_novo` NUNCA podem vir vazios. Sem eles a correção\n  é descartada, mesmo que o id esteja certo. `campo` é uma destas cinco palavras exatas:\n  valor, categoria, conta, descricao, data.\n- exclusao: "apaga o último" → `alvo.id`. Se estiver ambíguo qual é, pergunte. Aqui `campo` e\n  `valor_novo` não se aplicam: devolva string vazia nos dois.\n- orcamento: "meu esperado de comer fora é 300" → alvo.campo=categoria, alvo.valor_novo=valor.\n- recorrente: "todo mês pago 110 de academia" → alvo com nome/valor.\n- fatura: "quanto tá a fatura?", "quando fecha o cartão?" → responda em `resposta`.\n- relatorio: "me manda o painel", "como tô esse mês?".\n- fechar_mes: "fechou o mes", "virou o mes", "reinicia" → preencha intencao=fechar_mes.\n- confirmacao: ele está respondendo uma pergunta sua que está aberta (veja pendências abaixo).\n  Um "sim", "não", "foi 120", "é meu" isolado quase sempre é isso.\n- nenhuma: mandou áudio/texto que não tem nada a ver com dinheiro. Fique CALADO, lista vazia,\n  precisa_perguntar=false. Não force lançamento nunca.\n\n# Vários lançamentos num áudio só — regra tão importante quanto a da conta\nQuebre TUDO que foi dito em itens separados, um por gasto/receita. Nunca some valores diferentes\nnum item só, e nunca devolva só o primeiro que ouviu.\n- "gastei 40 de mercado na conta B e 23 de restaurante" → 2 itens. Cada item tem a SUA conta: o\n  mercado vai pra B (foi dito), o restaurante vai pra A (padrão). Um "conta B" dito no meio da\n  frase vale só pro item a que ele se refere — não contamina os outros.\n- "Igor, Kauan e Bruno me mandaram 5 reais ontem" → 3 receitas de R$ 5,00, uma por pessoa, com a\n  data de ontem. Várias pessoas + um valor = aquele valor de CADA UMA. Só devolva um item único\n  se ele disser que foi o total ("mandaram 5 no total", "juntaram 5 reais").\n- "30 no almoço, 15 no Uber e 200 no mercado" → 3 itens, contas A, A e B.\n- Itens repetidos são legítimos: "gastei 20 no Uber e mais 20 no Uber" → 2 itens iguais.\n- Gasto e receita no mesmo áudio: use intencao="gasto" e marque o `tipo` certo em cada item.\n- Se um item da lista precisar de pergunta e os outros não, devolva os que estão claros e\n  pergunte só sobre o duvidoso.\n\n# Números falados em português\n"trinta e cinco" → 35.00 | "trinta e cinco e cinquenta" → 35.50 | "cento e dez" → 110.00\n"duzentos e sessenta e um" → 261.00 | "mil e duzentos" → 1200.00 | "vinte e seis e noventa" → 26.90\n"três e cinquenta" → 3.50 | "cem conto" → 100.00 | "duas pila" → 2.00\nPROIBIDO INVENTAR VALOR. Sem número claro no áudio → precisa_perguntar=true e pergunte quanto foi.\nNunca chute um valor plausível.\n\n# Recorrentes que ele já cadastrou\n{recorrentes}\nÚNICA exceção à regra de nunca preencher valor: se ele citar um desses SEM falar o valor\n("yt pago", "paguei o spotify", "academia paga", "pagei o clube do ifood"), use o valor\ncadastrado acima, confianca="alta", e NÃO pergunte — ele já registrou esse número antes.\nSe ele disser um valor diferente do cadastrado, o valor DITO ganha sempre.\nApelidos: "yt"/"youtube" = YouTube Premium; "academia"/"gym" = Iron Gym;\n"clube do ifood"/"assinatura do ifood" = iFood Club.\n\n# Datas\nSem data dita → hoje. "ontem", "anteontem", "sexta passada", "dia 3" → resolva a partir de hoje.\nSempre YYYY-MM-DD. Nunca uma data futura.\n\n# Categorias e o vocabulário dele (vem das faturas reais)\n{categorias}\nMercado/Supermercado: Leve Max, Pantanal, Melo e Cuenca, Machadão, Comper, Assaí, Ramin, "rancho".\nCombustível: Auto Posto Guaicurus, Posto Benjamim, Ipiranga, Shell, Oshiro, "abasteci", "gasolina".\nMarmita (Fitfood): Fitfood, "marmita". | Transporte/App: Uber, 99.\nATENÇÃO — os dois iFood são categorias DIFERENTES e não podem se misturar:\n  · "iFood" = pedido de comida ("pedi ifood", "40 no ifood", "ifood de janta"). Gasto variável.\n  · "iFood Club" = a assinatura mensal de ~R$ 5,95 ("clube do ifood", "assinatura do ifood",\n    "mensalidade do ifood"). Custo fixo. Só use quando ele falar de clube/assinatura/mensalidade,\n    ou quando for a cobrança recorrente de valor baixo. Pedido de comida NUNCA é iFood Club.\nSaúde/Academia: Iron Gym, Strikers, farmácia, Droga Raia, Pague Menos, dentista.\nRestaurantes/Lanches: almoço, janta, lanche, pizza, açaí, café, padaria, sushi, hambúrguer.\n  Os daqui: Bentolanches, The Best Açaí, Padoca do Rei, Pão Dourado, Mycaohotdog, Dom Beer,\n  Old Mexican, Vinis Pizzaria, Kukao Sorvetes, Espeto/Spetos, Cacau Show.\nStreaming: Netflix, Spotify, YouTube, Disney, HBO, Prime. | Psicóloga: sessão, terapia, psicóloga.\nServiços online/Tech: Apple, Google, iCloud, Amazon, GitHub, Kabum. | Pix p/ pessoas: pix pra alguém.\nSe nada encaixar: Outros. Categoria só PROVÁVEL não é motivo pra perguntar — chute a melhor e siga.\n\n# Quando perguntar (e quando calar a boca)\nPERGUNTE só se: falta o valor; o áudio está cortado/inaudível; a exclusão está ambígua; ou a conta\nA vs B está genuinamente em aberto (regra 3). Uma pergunta curta, uma de cada vez.\nNÃO PERGUNTE por categoria duvidosa, por forma de pagamento, nem por data — chute o mais provável.\nPerguntar demais faz ele largar o sistema em duas semanas. Perguntar de menos faz o dado mentir.\n\n# Pagamento\nSó marque credito/debito/pix/dinheiro se ele disser. Caso contrário nao_informado.\n\n# Exemplos\n"gastei trinta e cinco reais no almoço hoje"\n→ intencao=gasto, [{{valor:35.0, tipo:gasto, conta:A, conta_origem:padrao_categoria,\n   categoria:"Restaurantes/Lanches", descricao:"almoço", data:{hoje}, pagamento:nao_informado, confianca:alta}}]\n\n"duzentos no leve max"\n→ conta B (mercado é padrão do pai, e 200 está dentro do limiar), confianca alta, sem pergunta.\n\n"abasteci oitenta reais, conta B"\n→ conta B com conta_origem="dito".\n\n"abasteci trezentos pra viagem"\n→ precisa_perguntar=true, pergunta="Combustível de R$ 300 — conta B ou é seu?", lancamentos=[].\n\n"gastei uma grana no mercado ontem"\n→ precisa_perguntar=true, pergunta="Quanto foi no mercado ontem?", lancamentos=[].\n\n"meu pai mandou trezentos e cinquenta"\n→ intencao=receita, [{{valor:350.0, tipo:receita, conta:B, categoria:"Outros", descricao:"ajuda do pai"}}]\n\n"gastei quarenta de mercado na conta B e vinte e três de restaurante"\n→ intencao=gasto, 2 itens:\n   [{{valor:40.0, conta:B, conta_origem:dito, categoria:"Mercado/Supermercado", descricao:"mercado"}},\n    {{valor:23.0, conta:A, conta_origem:padrao_categoria, categoria:"Restaurantes/Lanches",\n      descricao:"restaurante"}}]\n\n"Igor, Kauan e Bruno me mandaram cinco reais ontem"\n→ intencao=receita, 3 itens de valor 5.0, tipo=receita, conta=A, categoria "Outros",\n   descricao "Igor", "Kauan", "Bruno", data de ontem.\n\n"quanto eu já gastei de comer fora esse mês?"\n→ intencao=consulta, consulta={{periodo:mes, categoria:"Restaurantes/Lanches", conta:A}},\n   resposta preenchida com o número real da lista abaixo. lancamentos=[].\n\n"ó, depois eu te mando aquele arquivo que a gente tava vendo"\n→ intencao=nenhuma, lancamentos=[], precisa_perguntar=false. Não responda nada.\n\n# Estado atual (use pra responder consultas, corrigir e excluir)\nOrçamento: {orcamento}\nÚltimos lançamentos: {recentes}\nSemana da conta B: {semana}\nPendência aberta: {pendencia}\n'
DIAS = ['segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado', 'domingo']

def _contexto(orcamento, recentes, semana, pendencia, fatura, recorrentes):
    linhas = [f"id={l['id']} {l['data']} {brl(l['valor'])} [{l['conta']}] {l['categoria']} — {l['descricao']}" for l in recentes]
    if semana:
        s = f"acumulando desde {semana['ciclo']['inicio']}, já gastou {brl(semana['gasto'])} — sem teto até ele dizer quanto o pai mandou"
    else:
        s = 'período novo, nada gasto ainda'
    esperados = ', '.join((f"{i['categoria']}={i['esperado']}" for i in orcamento.get('A', {}).get('itens', [])))
    return {'hoje': hoje().isoformat(), 'diasem': DIAS[hoje().weekday()], 'fatura': fatura, 'padrao_b': ', '.join(sorted(PADRAO_CONTA_B)), 'limiares': ', '.join((f'{k} acima de R$ {v:.0f}' for k, v in orcamento.get('limiares', {}).items() if not k.startswith('_'))), 'categorias': ', '.join(CATEGORIAS), 'orcamento': f"renda A={orcamento.get('A', {}).get('renda')}, esperados: {esperados}; teto semanal B={orcamento.get('B', {}).get('teto_semanal')}", 'recentes': '\n'.join(linhas) or '(nenhum ainda)', 'semana': s, 'pendencia': pendencia or 'nenhuma', 'recorrentes': '\n'.join((f'''- {i['nome']}: R$ {float(i['valor']):.2f}, categoria "{i['categoria']}"''' for i in (recorrentes or {}).get('itens', []))) or '(nenhum cadastrado)'}

def _post(corpo: dict, api_key: str, schema: dict, tentativas=3) -> dict:
    modelos = [m for m in (MODELO, RESERVA) if m]
    ultimo = None
    for i, modelo in enumerate(modelos):
        try:
            return _post_modelo(corpo, api_key, schema, modelo, tentativas)
        except erros.ErroTemporario as e:
            ultimo = e
            if e.chave not in ('gemini-cota-dia', 'gemini-rpm') or i == len(modelos) - 1:
                raise
            print(f'{modelo} sem cota; tentando {modelos[i + 1]}')
    raise ultimo

def _post_modelo(corpo: dict, api_key: str, schema: dict, modelo: str, tentativas: int) -> dict:
    payload = json.dumps({'contents': [{'role': 'user', 'parts': corpo}], 'generationConfig': _config(modelo, schema)}).encode()
    ultimo = None
    for tentativa in range(tentativas):
        req = urllib.request.Request(URL.format(modelo) + f'?key={api_key}', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            return _extrair_texto(resp)
        except urllib.error.HTTPError as e:
            ultimo = erros.classificar_gemini(e.code, e.read().decode()[:800])
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            ultimo = erros.erro_rede('Gemini', str(e))
        except json.JSONDecodeError as e:
            ultimo = erros.erro_rede('Gemini', f'JSON inválido: {e}')
        except erros.ErroSistema as e:
            ultimo = e
        if isinstance(ultimo, erros.ErroPermanente) or tentativa == tentativas - 1:
            break
        if ultimo.chave == 'gemini-cota-dia':
            break
        time.sleep(ultimo.esperar if ultimo.chave == 'gemini-rpm' else 2 ** tentativa)
    raise ultimo

def _extrair_texto(resp: dict) -> dict:
    candidatos = resp.get('candidates') or []
    if not candidatos:
        motivo = (resp.get('promptFeedback') or {}).get('blockReason', 'sem candidatos')
        raise erros.erro_sem_resposta(str(motivo))
    c = candidatos[0]
    partes = (c.get('content') or {}).get('parts') or []
    if not partes or 'text' not in partes[0]:
        raise erros.erro_sem_resposta(str(c.get('finishReason', 'resposta vazia')))
    try:
        return json.loads(partes[0]['text'])
    except json.JSONDecodeError as e:
        raise erros.ErroTemporario(f'Gemini devolveu JSON quebrado: {e}', '🟡 O Gemini devolveu uma resposta malformada. Tento de novo no próximo ciclo.', chave='gemini-json')

def extrair(api_key, *, audio=None, mime='audio/ogg', texto=None, orcamento=None, recentes=(), semana=None, pendencia=None, fatura='', recorrentes=None) -> dict:
    prompt = PROMPT.format(**_contexto(orcamento or {}, list(recentes), semana, pendencia, fatura, recorrentes))
    partes = [{'text': prompt}]
    if audio:
        partes.append({'inline_data': {'mime_type': mime, 'data': base64.b64encode(audio).decode()}})
        partes.append({'text': 'Processe o áudio acima.'})
    else:
        partes.append({'text': f'Mensagem de texto do Igor: {texto!r}'})
    out = _post(partes, api_key, SCHEMA)
    out.setdefault('lancamentos', [])
    out.setdefault('precisa_perguntar', False)
    out.setdefault('intencao', 'nenhuma')
    if not audio and texto and (not out.get('transcricao')):
        out['transcricao'] = texto
    return out
REVISAO_SCHEMA = {'type': 'OBJECT', 'properties': {'sugestoes': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {'categoria': {'type': 'STRING'}, 'esperado_atual': {'type': 'NUMBER'}, 'esperado_sugerido': {'type': 'NUMBER'}, 'motivo': {'type': 'STRING'}}, 'required': ['categoria', 'esperado_atual', 'esperado_sugerido', 'motivo']}}, 'resumo': {'type': 'STRING'}}, 'required': ['sugestoes', 'resumo']}
PROMPT_REVISAO = 'Você revisa o orçamento mensal do Igor. Abaixo, o esperado que ele definiu e o\ngasto real dos últimos meses por categoria (só conta A).\n\nEsperado hoje: {esperado}\nReal por mês: {historico}\n\nProponha ajuste APENAS onde a diferença for consistente (3+ meses na mesma direção), não onde teve\num pico isolado. Aponte também assinatura que ele espera pagar mas não aparece cobrança há 2+ meses\n— pode ser assinatura fantasma, e é onde mora dinheiro fácil.\nSeja direto e curto. Você só PROPÕE; ele aprova por voz. Máximo 4 sugestões.\n'

def revisar_orcamento(api_key, esperado: dict, historico: dict) -> dict:
    corpo = [{'text': PROMPT_REVISAO.format(esperado=json.dumps(esperado, ensure_ascii=False), historico=json.dumps(historico, ensure_ascii=False))}]
    return _post(corpo, api_key, REVISAO_SCHEMA, tentativas=1)