"""Cliente mínimo do Telegram Bot API. Só stdlib — nada pra instalar."""

import json
import mimetypes
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import erros

API = "https://api.telegram.org/bot{token}/{metodo}"
ARQ = "https://api.telegram.org/file/bot{token}/{caminho}"

LIMITE_TEXTO = 4000  # o Telegram corta em 4096; sobra margem pras tags HTML


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.mudo = False  # liga quando o flood control bate: para de tentar falar

    def _call(self, metodo: str, tentativas=3, **params):
        dados = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
             for k, v in params.items() if v is not None}
        ).encode()

        ultimo = None
        for tentativa in range(tentativas):
            req = urllib.request.Request(API.format(token=self.token, metodo=metodo), data=dados)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.load(r)
                if not resp.get("ok"):
                    raise erros.classificar_telegram(metodo, 400, json.dumps(resp))
                return resp["result"]
            except urllib.error.HTTPError as e:
                ultimo = erros.classificar_telegram(metodo, e.code, e.read().decode()[:500])
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                ultimo = erros.erro_rede("Telegram", str(e))
            except json.JSONDecodeError as e:
                ultimo = erros.erro_rede("Telegram", f"resposta ilegível: {e}")

            # Só instabilidade merece nova tentativa; erro permanente não melhora esperando.
            if isinstance(ultimo, erros.ErroPermanente) or tentativa == tentativas - 1:
                break
            time.sleep(2 ** tentativa)
        raise ultimo

    # ---------------------------------------------------------- entrada

    def updates(self, offset: int):
        """Mensagens novas. O Telegram guarda o que você não leu por até 24h."""
        return self._call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])

    def baixar(self, file_id: str, limite: int) -> bytes:
        info = self._call("getFile", file_id=file_id)
        tamanho = info.get("file_size") or 0
        if tamanho > limite:
            raise erros.ErroPermanente(
                f"áudio de {tamanho / 1e6:.1f} MB passa do limite",
                f"🔴 Esse áudio tem {tamanho / 1e6:.1f} MB e não cabe no processamento. "
                "Manda um mais curto, ou digita o gasto.",
                chave="audio-grande",
            )
        url = ARQ.format(token=self.token, caminho=info["file_path"])
        ultimo = None
        for tentativa in range(3):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                ultimo = erros.classificar_telegram("download", e.code, e.read().decode()[:300])
                if isinstance(ultimo, erros.ErroPermanente):
                    break
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                # Baixar áudio é o passo mais frágil do caminho todo: uma falha de rede
                # aqui travava a fila inteira até o próximo ciclo.
                ultimo = erros.erro_rede("Telegram", str(e))
            if tentativa < 2:
                time.sleep(2 ** tentativa)
        raise ultimo

    # ---------------------------------------------------------- saída

    def enviar(self, texto: str, responder_a: int | None = None):
        """Nunca deixa o envio derrubar o run: o dado já está salvo, a mensagem é o extra."""
        if not texto or not texto.strip() or self.mudo:
            return None
        for pedaco in _fatiar(texto, LIMITE_TEXTO):
            try:
                self._call("sendMessage", chat_id=self.chat_id, text=pedaco,
                           parse_mode="HTML", disable_web_page_preview=True,
                           reply_to_message_id=responder_a)
            except erros.ErroSistema as e:
                if e.chave == "tg-flood":
                    self.mudo = True
                    return None
                # HTML malformado nosso não pode calar o bot: reenvia como texto puro.
                try:
                    self._call("sendMessage", chat_id=self.chat_id, text=_sem_tags(pedaco),
                               tentativas=1)
                except erros.ErroSistema:
                    print(f"não consegui enviar: {e}")
                    return None
            responder_a = None  # só o primeiro pedaço responde à mensagem original
        return True

    def documento(self, caminho: str, legenda: str = ""):
        if self.mudo or not os.path.exists(caminho):
            return None
        limite = "----" + uuid.uuid4().hex
        nome = os.path.basename(caminho)
        with open(caminho, "rb") as f:
            conteudo = f.read()
        tipo = mimetypes.guess_type(nome)[0] or "application/octet-stream"

        corpo = b""
        for chave, valor in (("chat_id", self.chat_id), ("caption", legenda)):
            corpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"{chave}\"\r\n\r\n"
                      f"{valor}\r\n").encode()
        corpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"document\"; "
                  f"filename=\"{nome}\"\r\nContent-Type: {tipo}\r\n\r\n").encode()
        corpo += conteudo + f"\r\n--{limite}--\r\n".encode()

        req = urllib.request.Request(
            API.format(token=self.token, metodo="sendDocument"),
            data=corpo,
            headers={"Content-Type": f"multipart/form-data; boundary={limite}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # painel é conveniência, não pode quebrar o run
            print(f"não consegui mandar o painel: {e}")
            return None


def _sem_tags(t: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", t)


def _fatiar(texto: str, limite: int):
    """Quebra em pedaços respeitando quebras de linha, pra não cortar tag no meio."""
    if len(texto) <= limite:
        return [texto]
    pedacos, atual = [], ""
    for linha in texto.split("\n"):
        if len(atual) + len(linha) + 1 > limite:
            if atual:
                pedacos.append(atual)
            atual = linha[:limite]
        else:
            atual = f"{atual}\n{linha}" if atual else linha
    if atual:
        pedacos.append(atual)
    return pedacos


def extrair_midia(msg: dict) -> tuple[str | None, str]:
    """(file_id, mime) do áudio da mensagem, se houver."""
    if "voice" in msg:
        return msg["voice"]["file_id"], msg["voice"].get("mime_type", "audio/ogg")
    if "audio" in msg:
        return msg["audio"]["file_id"], msg["audio"].get("mime_type", "audio/mpeg")
    if "video_note" in msg:
        return msg["video_note"]["file_id"], "video/mp4"
    return None, ""
