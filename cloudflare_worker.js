/**
 * Cloudflare Worker: O "Carteiro" do Telegram
 * Ele pega a mensagem do Telegram e salva no GitHub, acionando o processamento.
 *
 * Como configurar no Cloudflare:
 * 1. Crie o Worker e cole este código.
 * 2. Em Settings -> Variables and Secrets, adicione a secret: GH_TOKEN.
 *    (O valor do GH_TOKEN é o token do GitHub com acesso de escrita ao repositório).
 */

const REPO = "igormichalski/financas"; // Ajuste se necessário

export default {
  async fetch(request, env) {
    // 1. Só aceita requisições POST do Telegram
    if (request.method !== "POST") {
      return new Response("OK");
    }

    try {
      const body = await request.json();
      
      // Se não for uma mensagem real (ex: edição, callback), ignoramos.
      if (!body.update_id || !body.message) {
        return new Response("OK");
      }

      const updateId = body.update_id;
      
      // 2. Prepara o conteúdo da mensagem para salvar no GitHub (Base64)
      const content = btoa(unescape(encodeURIComponent(JSON.stringify(body))));
      
      const ghHeaders = {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "User-Agent": "Cloudflare-Telegram-Worker",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28"
      };

      // 3. Salva a mensagem como um arquivo único na pasta 'inbox/'
      await fetch(`https://api.github.com/repos/${REPO}/contents/inbox/${updateId}.json`, {
        method: "PUT",
        headers: ghHeaders,
        body: JSON.stringify({
          message: `Nova mensagem ${updateId}`,
          content: content
        })
      });

      // 4. Dá o "Play" na Action (sync.yml) para o GitHub processar a caixa de entrada
      await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/sync.yml/dispatches`, {
        method: "POST",
        headers: ghHeaders,
        body: JSON.stringify({
          ref: "main"
        })
      });

      // 5. Devolve 200 OK para o Telegram saber que recebemos
      return new Response("OK");
      
    } catch (error) {
      console.error(error);
      return new Response("OK");
    }
  }
};
