/**
 * Cloudflare Worker: O "Carteiro" do Telegram
 * Ele pega a mensagem do Telegram e salva no GitHub, acionando o processamento.
 *
 * Como configurar no Cloudflare:
 * 1. Crie o Worker e cole este código.
 * 2. Em Settings -> Variables and Secrets, adicione as secrets:
 *    - GH_TOKEN:   token do GitHub com acesso de escrita ao repositório.
 *    - TG_SECRET:  uma senha qualquer que VOCÊ inventa (ex.: 32 caracteres aleatórios).
 *    - TG_CHAT_ID: o seu chat id no Telegram.
 *    - TG_TOKEN:   o token do bot (o mesmo do TELEGRAM_TOKEN no GitHub). Opcional:
 *                  serve só pra reagir 👀 na hora, avisando que a mensagem chegou
 *                  enquanto o Actions sobe. Sem ele, tudo funciona igual, só sem a reação.
 * 3. Registre o webhook mandando a MESMA senha do TG_SECRET:
 *
 *      curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
 *        -d "url=https://<seu-worker>.workers.dev" \
 *        -d "secret_token=<o mesmo valor do TG_SECRET>"
 *
 *    Sem isso a URL do Worker é um endpoint aberto: qualquer um que descobrir o
 *    endereço grava arquivo no seu repositório e dispara Actions à vontade.
 */

const REPO = "igormichalski/financas"; // Ajuste se necessário

export default {
  async fetch(request, env, ctx) {
    // 1. Só aceita requisições POST do Telegram
    if (request.method !== "POST") {
      return new Response("OK");
    }

    // 2. Prova que quem chamou é o Telegram, e não alguém que achou a URL.
    //    Responder 401 aqui é de propósito: o Telegram desiste desse update em vez
    //    de reentregar pra sempre, e a tentativa não vira arquivo no repositório.
    if (!env.TG_SECRET) {
      // Some no log do Worker se você esquecer de cadastrar a secret. Sem isso o bot
      // fica surdo e não é nada óbvio por quê.
      console.error("TG_SECRET não está configurada — recusando tudo. "
                    + "Cadastre a secret e refaça o setWebhook com o mesmo valor.");
      return new Response("worker sem TG_SECRET", { status: 401 });
    }
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TG_SECRET) {
      return new Response("não autorizado", { status: 401 });
    }

    try {
      const body = await request.json();

      // Se não for uma mensagem real (ex: edição, callback), ignoramos.
      if (!body.update_id || !body.message) {
        return new Response("OK");
      }

      // 3. Só a SUA conversa entra no repositório. O sync.py também filtra por
      //    chat_id, mas lá já é tarde: o commit e o run já teriam acontecido.
      if (env.TG_CHAT_ID && String(body.message?.chat?.id) !== String(env.TG_CHAT_ID)) {
        return new Response("OK");
      }

      const updateId = body.update_id;
      const texto = (body.message.text || body.message.caption || "").trim();

      // 3.2. Atalho do painel. O painel.html JÁ é um artefato pronto no repositório,
      //      gerado pelo último sync — então pedir "painel" é leitura pura: não muda
      //      ledger, não precisa do Gemini, não precisa do Actions. Mandar direto daqui
      //      leva ~2s em vez de ~20s.
      //
      //      Casa só com a palavra sozinha (com ou sem barra/acento). Qualquer frase
      //      maior segue o caminho normal, porque aí pode ser outra intenção
      //      ("me manda o painel e lança 30 de almoço") e adivinhar isso é do Gemini,
      //      não meu. Errar pra menos aqui só custa 20s; errar pra mais perde lançamento.
      if (env.TG_TOKEN && /^\/?(painel|relat[oó]rio)$/i.test(texto)) {
        ctx.waitUntil(mandarPainel(env, body.message.chat.id));
        return new Response("OK");
      }

      // 3.5. Feedback imediato. O GitHub Actions leva ~20s pra subir um runner, e
      //      silêncio nesse tempo parece que o bot morreu. A reação chega em menos de
      //      1s e não polui a conversa como uma mensagem de "processando..." faria.
      //
      //      SEM await: a reação é enfeite, e esperar por ela atrasava o PUT e o
      //      dispatch, que são o caminho crítico. Medido: com await, o Worker levava
      //      2,9s pra responder. ctx.waitUntil deixa a requisição terminar em segundo
      //      plano depois da resposta, sem o runtime matá-la no meio.
      const reagir = (env.TG_TOKEN && body.message?.message_id)
        ? fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/setMessageReaction`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              chat_id: body.message.chat.id,
              message_id: body.message.message_id,
              reaction: [{ type: "emoji", emoji: "👀" }]
            })
          }).catch((e) => console.error("reação falhou:", e))
        : null;
      if (reagir) ctx.waitUntil(reagir);

      // 4. Prepara o conteúdo da mensagem para salvar no GitHub (Base64)
      const content = btoa(unescape(encodeURIComponent(JSON.stringify(body))));

      const ghHeaders = {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "User-Agent": "Cloudflare-Telegram-Worker",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
      };

      // 5. Salva a mensagem como um arquivo único na pasta 'inbox/'
      const put = await fetch(
        `https://api.github.com/repos/${REPO}/contents/inbox/${updateId}.json`, {
          method: "PUT",
          headers: ghHeaders,
          body: JSON.stringify({
            message: `Nova mensagem ${updateId}`,
            content: content
          })
        });

      // 422 = o arquivo já existe, ou seja o Telegram reentregou o mesmo update.
      // Nesse caso está tudo certo e o run que já foi disparado dá conta.
      // Qualquer outra falha precisa devolver erro: se engolirmos e responder "OK",
      // o Telegram não reentrega e a mensagem se perde calada.
      if (!put.ok && put.status !== 422) {
        const detalhe = await put.text();
        console.error(`PUT inbox/${updateId}.json falhou: ${put.status} ${detalhe}`);
        return new Response("falha ao gravar", { status: 500 });
      }

      // 6. Dá o "Play" na Action (sync.yml) para o GitHub processar a caixa de entrada.
      //    Se o dispatch falhar, a mensagem NÃO se perde — ela já está na inbox e o
      //    cron da meia em meia hora pega. Por isso aqui a falha só é registrada.
      const disparo = await fetch(
        `https://api.github.com/repos/${REPO}/actions/workflows/sync.yml/dispatches`, {
          method: "POST",
          headers: ghHeaders,
          body: JSON.stringify({ ref: "main" })
        });
      if (!disparo.ok) {
        console.error(`dispatch falhou: ${disparo.status} ${await disparo.text()}`);
      }

      // 7. Devolve 200 OK para o Telegram saber que recebemos
      return new Response("OK");

    } catch (error) {
      console.error(error);
      // Erro inesperado devolve 500 pra que o Telegram reentregue depois.
      return new Response("erro", { status: 500 });
    }
  }
};

/**
 * Manda o painel.html que já está no repositório, direto pro Telegram.
 *
 * Roda dentro de ctx.waitUntil, depois do 200 pro Telegram — então pode levar alguns
 * segundos sem o Telegram achar que o webhook está lento e reentregar.
 */
async function mandarPainel(env, chatId) {
  try {
    const r = await fetch(
      `https://api.github.com/repos/${REPO}/contents/painel.html?ref=main`, {
        headers: {
          "Authorization": `Bearer ${env.GH_TOKEN}`,
          "User-Agent": "Cloudflare-Telegram-Worker",
          "Accept": "application/vnd.github.raw",
          "X-GitHub-Api-Version": "2022-11-28"
        }
      });
    if (!r.ok) {
      console.error(`não consegui ler o painel: ${r.status} ${await r.text()}`);
      return;
    }
    const html = await r.arrayBuffer();

    // O painel é gerado pelo último sync. Se você acabou de lançar algo e o sync ainda
    // não rodou, este arquivo é de antes — por isso a legenda diz de quando ele é, em
    // vez de deixar você achar que o número já inclui o gasto de agora.
    const quando = new Date().toLocaleString("pt-BR", { timeZone: "America/Campo_Grande" });
    const form = new FormData();
    form.append("chat_id", String(chatId));
    form.append("caption", `📊 Painel (versão salva no repositório · consultado ${quando})`);
    // Blob + nome no terceiro argumento, e não `new File(...)`: é a forma que o runtime
    // do Workers garante em qualquer versão do compatibility date.
    form.append("document", new Blob([html], { type: "text/html" }), "painel.html");

    const envio = await fetch(
      `https://api.telegram.org/bot${env.TG_TOKEN}/sendDocument`, { method: "POST", body: form });
    if (!envio.ok) {
      console.error(`sendDocument falhou: ${envio.status} ${await envio.text()}`);
    }
  } catch (e) {
    console.error("atalho do painel falhou:", e);
  }
}
