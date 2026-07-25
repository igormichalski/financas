# Controle financeiro por áudio

Você fala no Telegram, o GitHub Actions processa, o ledger vive neste repositório.
Nada roda no seu computador. Custo: R$ 0,00.

```
áudio no Telegram  →  Actions (de 30 em 30 min)  →  Gemini  →  lancamentos.csv  →  painel.html
                                                                      ↓
                                                      confirmação de volta no Telegram
```

## Como funciona (e o que fica ligado)

**Nada fica ligado.** Seu computador pode estar desligado. O runner do GitHub vive uns 40
segundos, faz o trabalho e morre. Entre um ciclo e outro o sistema não existe — só os
arquivos parados neste repositório.

```
   VOCÊ                    TELEGRAM                 GITHUB ACTIONS              GEMINI
   ────                    ────────                 ──────────────              ──────
     │
     │  🎙 áudio/texto         │
     ├───────────────────────► │
     │                         │  guarda até 24h
     │                         │  (nada acordado aqui)
     │                         │
     │                         │        ⏰ a cada 30 min (07h–00h)
     │                         │        o cron ACORDA o runner
     │                         │              │
     │                         │ ◄────────────┤  1. getUpdates
     │                         ├────────────► │     (marca como lido)
     │                         │              │
     │                         │              │  2. grava em fila.json ◄── trava anti-perda
     │                         │              │     e commita
     │                         │              │
     │                         │              ├──────────────────────────► │  3. áudio → JSON
     │                         │              │ ◄──────────────────────────┤     (transcreve +
     │                         │              │                                   estrutura)
     │                         │              │  4. lancamentos.csv
     │                         │              │     painel.html
     │                         │              │     git commit + push
     │                         │              │
     │  ✅ confirmação         │ ◄────────────┤  5. sendMessage
     │ ◄───────────────────────┤              │
     │                         │              │
     │                         │        💤 runner MORRE
```

| Onde | O quê | Some se... |
|---|---|---|
| Telegram (servidor) | suas mensagens não lidas | passar 24h |
| GitHub (repo privado) | ledger, orçamento, fila, painel | nunca |
| GitHub (secrets) | as 3 chaves | você apagar |
| Runner | o código rodando | sempre, em ~40s |
| Seu computador | nada | — |

A `fila.json` existe por causa da primeira linha dessa tabela: ela move suas mensagens do
lugar que esquece em 24h para o lugar que nunca esquece, **antes** de tentar processar.

### Por que não existe um `/rodar` no Telegram

Entre um ciclo e outro não há nada escutando — ler o Telegram é justamente o que o runner
faz quando acorda. Um `/rodar` só seria lido no ciclo seguinte, que é o que você queria
pular. Pra forçar agora, veja a seção abaixo.

## As duas carteiras

|  | **Conta A** | **Conta B** |
|---|---|---|
| Dinheiro | seu salário | ajuda do pai |
| Teto | R$ 1.600/mês | o que ele mandou, por semana |
| Ciclo | mensal | reseta quando ele manda |
| Padrão | todo o resto | mercado, combustível, marmita |

Diga **"conta B"** pra forçar a carteira do pai e **"conta A"** pra forçar a sua — a
palavra dita sempre ganha do padrão. Se o valor destoar do seu histórico
(combustível acima de R$ 110, mercado acima de R$ 160 — calibrado dos seus 18 meses
reais), o bot pergunta antes de gravar em vez de chutar.

## Setup (uma vez, ~15 min)

### 1. Bot do Telegram

1. Fale com [@BotFather](https://t.me/BotFather) → `/newbot` → guarde o **token**.
2. **`/setprivacy` → escolha o bot → `Disable`.** Sem isso o bot em grupo só enxerga
   mensagens que começam com `/`, e todo áudio passa batido. É a pegadinha nº 1.
3. Crie um grupo só de finanças, adicione o bot, mande um "oi".
4. Abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id`
   (vem negativo, tipo `-4912345678`).

### 2. Chave do Gemini

[aistudio.google.com](https://aistudio.google.com/apikey) → criar chave. Sem cartão.
Free tier: ~1.500 requisições/dia; você vai usar ~10.

### 3. Repositório

Crie um repositório **privado**, suba esta pasta, e em
**Settings → Secrets and variables → Actions** cadastre:

| Secret | Valor |
|---|---|
| `TELEGRAM_TOKEN` | o token do BotFather |
| `TELEGRAM_CHAT_ID` | o id do grupo (com o menos) |
| `GEMINI_API_KEY` | a chave do AI Studio |

Depois: **Actions → sync → Run workflow** pra testar na hora.

### 4. Histórico (opcional)

```bash
python3 importar_historico.py /caminho/da/pasta/Nubank
```

Lê `faturas/Nubank_*.csv` e `NU_*.csv`, importa os 18 meses e recalibra os limiares
com os seus números. O painel nasce com contexto em vez de vazio.

## Como usar (tudo por voz ou texto)

| Você diz | Acontece |
|---|---|
| "gastei 35 no almoço" | lança na conta A |
| "duzentos no Leve Max" | lança na conta B (mercado é padrão do pai) |
| "abasteci 80, conta B" | força a conta B |
| "abasteci 300 pra viagem" | o bot pergunta de qual conta antes de gravar |
| "meu pai mandou 350" | abre uma semana nova da conta B com teto 350 |
| "quanto gastei de comer fora esse mês?" | responde na hora, não grava nada |
| "aquele almoço foi 45, não 35" | corrige a linha |
| "apaga o último" | pede confirmação antes |
| "meu esperado de comer fora é 300" | ajusta o orçamento |
| "todo mês pago 110 de academia" | vira recorrente (mas nunca lança sozinho) |
| "quanto tá a fatura?" | total da fatura aberta e quando fecha |
| "me manda o painel" | manda o `painel.html` no grupo |

Áudio que não tem nada a ver com dinheiro: o bot fica **calado**, de propósito.

## O cartão

Fecha dia **15**, vence dia **22**. Compra dia 15 é paga no dia 22 do mesmo mês;
compra dia 16 só é paga no dia 22 do mês seguinte. O sistema separa os dois
regimes: **competência** (quanto você torrou) e **caixa** (quanto sai da conta).
Entre os dias 13 e 15 o bot lembra que a fatura vai fechar.

## Os arquivos

| Arquivo | O que é |
|---|---|
| `lancamentos.csv` | o ledger — a fonte da verdade |
| `orcamento.json` | as duas carteiras, os valores esperados, os limiares |
| `recorrentes.json` | assinaturas conhecidas (pra cobrar, não pra lançar sozinho) |
| `semanas.json` | ciclos da conta B |
| `painel.html` | o painel, regenerado a cada mudança |
| `revisar.csv` | o que a IA não teve certeza e você ignorou |

**Editar em massa:** abra `lancamentos.csv` ou `orcamento.json` pela interface web do
GitHub. Já é privada, autenticada e versionada. Pra corrigir 20 categorias de uma vez
isso é melhor que voz.

## Forçar um sync agora

O cron roda de 30 em 30 min (07h–00h). Pra não esperar:

```bash
./rodar.sh          # processa e manda o painel
./rodar.sh rapido   # só processa
```

Longe do computador? **App do GitHub no celular** → repositório `financas` → aba Actions →
workflow `sync` → **Run workflow**. Mesmo efeito, dois toques.

## Nada é processado duas vezes

Duas travas independentes, e a segunda existe justamente pra cobrir falha da primeira:

1. **Offset do Telegram.** Pedir mensagens a partir de um `update_id` funciona como recibo —
   o Telegram nunca reentrega o que já foi confirmado. Ele só avança depois que a mensagem
   está gravada na fila.
2. **Marca no lançamento.** Cada linha guarda `mensagem#posição` em `msg_id`. Se a mesma
   mensagem reaparecer (job morto no meio, restauração de backup, run duplicado), ela é
   reconhecida e ignorada.

Run sem mensagem nova não faz nada: zero requisição ao Gemini, zero commit, e o bot fica
calado. Só custa os poucos segundos de runner.

## Testar sem gastar nada

```bash
python3 testes.py        # roteamento A/B, fatura, ciclo semanal, intenções
python3 painel.py        # regera o painel a partir do CSV
```

## Quando algo dá errado

A regra: **nenhuma mensagem sua se perde, nunca.** Tudo que chega do Telegram vai
primeiro pra `fila.json`, que é versionada no git. Só sai da fila quando foi processada
com sucesso. Se o Gemini ficar dois dias fora do ar, a fila espera — inclusive o que
passaria das 24h que o Telegram guarda.

| O que acontece | O que o sistema faz | O que você vê |
|---|---|---|
| Cota do Gemini por minuto | espera e retoma no mesmo run | aviso, uma vez a cada 6h |
| Cota diária do Gemini | fila congela até virar o dia | aviso explicando |
| Chave do Gemini inválida | fila congela | 🔴 avisa qual secret trocar |
| Gemini instável (5xx) | 3 tentativas com recuo | aviso se persistir |
| Filtro de conteúdo bloqueou | pula a mensagem, anota em `revisar.csv` | 🔴 avisa |
| Token do bot inválido | para | 🔴 avisa qual secret trocar |
| Bot removido do grupo | para | 🔴 avisa |
| Flood control do Telegram | fica mudo no resto do run | nada (evita piorar) |
| Áudio acima de 18 MB | pula | 🔴 pede áudio menor |
| Mensagem acima de 4096 chars | fatia em pedaços | nada |
| Sem internet | fila preservada | aviso |
| `state.json` corrompido | renomeia pra `.corrompido` e recomeça | nada |
| Linha quebrada no CSV | pula a linha | nada |
| Job morto no meio da escrita | escrita atômica, arquivo nunca fica pela metade | nada |
| Uma mensagem falha 3 vezes | descarta pra não travar a fila | ⚠️ avisa |
| Erro que ninguém previu | avisa com o tipo do erro | 🔴 avisa |

Avisos repetidos são agrupados: o mesmo erro não aparece mais de uma vez a cada 6 horas.

## Limites honestos

- O Telegram guarda mensagem não lida por **24h**. Se o Actions ficar mais de um dia
  fora do ar, o que passou disso se perde.
- O free tier da API do Gemini usa o conteúdo enviado pra melhorar os produtos do
  Google. São áudios do tipo "gastei 35 no almoço" — baixa sensibilidade, mas você
  precisa saber.
- O cron do GitHub Actions atrasa alguns minutos sob carga. Irrelevante aqui.
- Nada é lançado automaticamente. Recorrente esquecido vira pergunta, nunca
  transação inventada.
