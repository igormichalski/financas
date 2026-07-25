"""Classificação de falhas e as mensagens que o Igor vê quando algo quebra.

A distinção que importa: **dá pra tentar de novo ou não?**
- ErroTemporario  → a fila para, nada é perdido, o próximo run retoma.
- ErroPermanente  → tentar de novo não adianta; descarta o item e avisa o que fazer.

Falha silenciosa aqui significa gasto perdido, então todo caminho de erro termina
numa mensagem no Telegram — em português, dizendo o que houve e o que fazer.
"""


class ErroSistema(Exception):
    """Base. `aviso` é o texto que vai pro Telegram."""

    def __init__(self, mensagem, aviso=None, chave=None, esperar=0):
        super().__init__(mensagem)
        self.aviso = aviso or mensagem
        self.chave = chave or self.__class__.__name__  # pra não repetir o mesmo aviso
        self.esperar = esperar  # segundos sugeridos pelo serviço (429)


class ErroTemporario(ErroSistema):
    """Passa sozinho: cota, instabilidade, rede. A fila espera o próximo run."""


class ErroPermanente(ErroSistema):
    """Só passa com você mexendo em alguma coisa."""


# ---------------------------------------------------------------- Gemini


def classificar_gemini(codigo: int, corpo: str) -> ErroSistema:
    c = (corpo or "").lower()

    if codigo == 429:
        if "quota" in c or "resource_exhausted" in c or "exceeded" in c:
            return ErroTemporario(
                f"cota do Gemini esgotada ({codigo})",
                "🟡 <b>Cota do Gemini esgotada por enquanto.</b>\n"
                "Suas mensagens estão guardadas na fila — nada se perdeu. Vou tentar de novo "
                "sozinho no próximo ciclo (até 30 min).\n"
                "<i>Se isso virar rotina, o free tier está apertado pro seu uso.</i>",
                chave="gemini-cota", esperar=60,
            )
        return ErroTemporario(
            f"limite de requisições do Gemini ({codigo})",
            "🟡 Muitas requisições seguidas ao Gemini. Guardei tudo na fila e tento daqui a pouco.",
            chave="gemini-rate", esperar=30,
        )

    if codigo in (401, 403) or "api_key_invalid" in c or "api key not valid" in c:
        return ErroPermanente(
            f"chave do Gemini rejeitada ({codigo})",
            "🔴 <b>A chave do Gemini foi rejeitada.</b>\n"
            "Gere outra em aistudio.google.com/apikey e atualize o secret "
            "<code>GEMINI_API_KEY</code> no GitHub.\n"
            "Suas mensagens ficam na fila até isso ser resolvido.",
            chave="gemini-chave",
        )

    if codigo == 404:
        return ErroPermanente(
            f"modelo do Gemini não encontrado ({codigo})",
            "🔴 O modelo do Gemini não existe mais com esse nome. "
            "Ajuste a variável <code>GEMINI_MODELO</code>.",
            chave="gemini-modelo",
        )

    if codigo >= 500 or "unavailable" in c or "overloaded" in c:
        return ErroTemporario(
            f"Gemini instável ({codigo})",
            "🟡 O Gemini está instável agora. Fila preservada, tento de novo no próximo ciclo.",
            chave="gemini-instavel", esperar=30,
        )

    if codigo == 400:
        return ErroPermanente(
            f"requisição rejeitada pelo Gemini ({codigo}): {corpo[:200]}",
            "🔴 O Gemini recusou essa mensagem (formato de áudio ou tamanho). "
            "Pulei ela e anotei em <code>revisar.csv</code> — pode remandar digitando.",
            chave="gemini-400",
        )

    return ErroTemporario(
        f"Gemini {codigo}: {corpo[:200]}",
        f"🟡 Erro inesperado no Gemini ({codigo}). Fila preservada, tento de novo.",
        chave=f"gemini-{codigo}",
    )


def erro_sem_resposta(motivo: str) -> ErroSistema:
    """O Gemini respondeu mas sem conteúdo — filtro de segurança ou corte por tamanho."""
    m = (motivo or "").upper()
    if "SAFETY" in m or "BLOCK" in m or "PROHIBITED" in m:
        return ErroPermanente(
            f"resposta bloqueada pelo Gemini ({motivo})",
            "🔴 O Gemini bloqueou essa mensagem por filtro de conteúdo. "
            "Pulei ela — se era gasto de verdade, manda digitado.",
            chave="gemini-safety",
        )
    if "MAX_TOKENS" in m:
        return ErroPermanente(
            "resposta cortada por tamanho",
            "🔴 Esse áudio gerou resposta longa demais e foi cortada. "
            "Tenta quebrar em áudios menores.",
            chave="gemini-tamanho",
        )
    return ErroTemporario(
        f"Gemini não devolveu conteúdo ({motivo})",
        "🟡 O Gemini devolveu vazio. Guardei na fila e tento de novo.",
        chave="gemini-vazio",
    )


# ---------------------------------------------------------------- Telegram


def classificar_telegram(metodo: str, codigo: int, corpo: str) -> ErroSistema:
    c = (corpo or "").lower()

    if codigo == 401:
        return ErroPermanente(
            "token do bot inválido",
            "🔴 O token do bot foi rejeitado. Atualize <code>TELEGRAM_TOKEN</code> no GitHub.",
            chave="tg-token",
        )

    if codigo == 403:
        return ErroPermanente(
            "bot sem acesso ao grupo",
            "🔴 O bot foi removido do grupo ou perdeu permissão. Adicione ele de novo.",
            chave="tg-acesso",
        )

    if codigo == 429:
        espera = 30
        achou = [int(s) for s in c.replace(":", " ").replace(",", " ").split()
                 if s.isdigit() and "retry_after" in c]
        if achou:
            espera = min(achou[-1], 300)
        return ErroTemporario(
            "flood control do Telegram",
            "🟡 Mandei mensagem demais de uma vez. Segurando o resto pro próximo ciclo.",
            chave="tg-flood", esperar=espera,
        )

    if codigo == 400:
        if "file is too big" in c:
            return ErroPermanente(
                "arquivo grande demais",
                "🔴 Esse áudio passou de 20 MB, que é o limite do Telegram pra bots. "
                "Manda um mais curto.",
                chave="tg-arquivo-grande",
            )
        if "chat not found" in c:
            return ErroPermanente(
                "grupo não encontrado",
                "🔴 Não achei o grupo. Confira o secret <code>TELEGRAM_CHAT_ID</code>.",
                chave="tg-chat",
            )
        return ErroPermanente(
            f"Telegram recusou {metodo}: {corpo[:200]}",
            None,  # erro de formatação nossa: não vale poluir o grupo
            chave=f"tg-400-{metodo}",
        )

    if codigo >= 500:
        return ErroTemporario(
            f"Telegram instável ({codigo})",
            None,
            chave="tg-instavel", esperar=20,
        )

    return ErroTemporario(f"Telegram {metodo} {codigo}: {corpo[:200]}", None,
                          chave=f"tg-{codigo}")


def erro_rede(servico: str, detalhe: str) -> ErroTemporario:
    return ErroTemporario(
        f"rede indisponível ({servico}): {detalhe}",
        f"🟡 Sem conexão com {servico} agora. Fila preservada, tento de novo no próximo ciclo.",
        chave=f"rede-{servico}", esperar=20,
    )
