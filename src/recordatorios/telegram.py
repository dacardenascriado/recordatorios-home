"""Cliente mínimo de la Bot API de Telegram."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

API_BASE = "https://api.telegram.org"
TIMEOUT_SECONDS = 20.0
MAX_ATTEMPTS = 3

# Límites de sendPoll. Los comprueba el loader, para que un YAML que Telegram
# rechazaría no llegue nunca a main: acá el fallo sería un recordatorio que no
# sale, y de esos ya tuvimos.
POLL_QUESTION_MAX = 300
POLL_OPTION_MAX = 100
POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 12


class TelegramError(RuntimeError):
    """Fallo al hablar con la Bot API."""


class TelegramConflict(TelegramError):
    """Otro proceso está leyendo las actualizaciones de este mismo bot.

    Telegram admite un solo lector de `getUpdates` a la vez y le contesta 409 al
    segundo. Acá pasa cuando un bloque de `tick-loop` y una corrida suelta de
    `tick.yml` se cruzan, y no es un fallo: el otro runner ya está haciendo el
    trabajo. Tiene su propia clase para que quien llama pueda dejarlo pasar sin
    confundirlo con un token malo o un chat inexistente.
    """


def redact(text: str, token: str | None) -> str:
    """Borra el token de un texto antes de que llegue a un log.

    El token va en la URL (`/bot<TOKEN>/sendMessage`), y algunas excepciones de
    httpx incluyen la URL en su mensaje. Los logs de Actions son públicos en un
    repo público: GitHub enmascara los secrets que reconoce, pero no conviene
    depender de eso pudiendo no escribirlo nunca.
    """
    if not token:
        return text
    return text.replace(token, "***")


class Sender(Protocol):
    """Lo que tick.py necesita de un emisor. Los tests inyectan uno falso."""

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        silent: bool = False,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]: ...

    def send_poll(
        self,
        chat_id: str,
        question: str,
        options: Sequence[str],
        silent: bool = False,
    ) -> dict[str, Any]: ...


class Reader(Protocol):
    """Lo que hace falta para enterarse de lo que contestó la gente."""

    def get_updates(
        self,
        offset: int | None = None,
        allowed_updates: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


class Bot(Sender, Reader, Protocol):
    """Las dos mitades juntas: leer las respuestas y avisarlas al chat."""


class TelegramSender:
    def __init__(self, token: str, base_url: str = API_BASE) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        silent: bool = False,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if silent:
            payload["disable_notification"] = True
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            # Si borraron el mensaje original, el aviso sale igual sin quedar
            # colgado de nada. Sin esto Telegram devuelve 400 y el aviso —que
            # es la parte que importa— no llegaría por un detalle de formato.
            payload["allow_sending_without_reply"] = True
        return self._call("sendMessage", payload)

    def send_poll(
        self,
        chat_id: str,
        question: str,
        options: Sequence[str],
        silent: bool = False,
    ) -> dict[str, Any]:
        """Manda una encuesta abierta, para que alguien confirme que se encarga.

        `is_anonymous=False` es el punto de todo el asunto: una encuesta anónima
        diría cuántos contestaron, no quién, y lo que se quiere saber es
        justamente si contestó la persona a la que le toca.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question,
            # InputPollOption, que es el tipo que documenta la Bot API desde la
            # 7.3. Una lista de textos pelados todavía se acepta por
            # compatibilidad, pero api.telegram.org siempre corre la versión
            # nueva y acá un rechazo es un recordatorio que no llega.
            "options": [{"text": texto} for texto in options],
            "is_anonymous": False,
        }
        if silent:
            payload["disable_notification"] = True
        return self._call("sendPoll", payload)

    def get_updates(
        self,
        offset: int | None = None,
        allowed_updates: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Lee lo que llegó desde la última confirmación. No espera (timeout 0).

        `offset` hace dos cosas de una vez: pide a partir de ese `update_id` y,
        con eso mismo, confirma todos los anteriores — Telegram los borra y no
        vuelven a llegar. Ese es todo el cursor que hay que llevar, así que no
        hace falta guardarlo en la base (y por lo tanto no hay que despertarla
        para leer las respuestas).

        Lo no confirmado se guarda 24 h en los servidores de Telegram: una
        corrida que falle a mitad de camino no pierde nada, lo retoma la
        siguiente.
        """
        payload: dict[str, Any] = {"timeout": 0, "limit": limit}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = list(allowed_updates)
        resultado = self._call("getUpdates", payload)
        return resultado if isinstance(resultado, list) else []

    def get_me(self) -> dict[str, Any]:
        """Datos del bot. Sirve para comprobar que el token vale."""
        return self._call("getMe", {})

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Datos del chat. Comprueba que el bot lo alcanza, sin mandar nada."""
        return self._call("getChat", {"chat_id": chat_id})

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        last_error = "sin intentos"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                    response = client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"error de red: {exc}"
            else:
                body = _json_or_none(response)

                if response.status_code == 200 and body and body.get("ok"):
                    return body.get("result", {})

                description = (body or {}).get("description", response.text[:200])
                last_error = f"HTTP {response.status_code}: {description}"

                # Dos lectores de getUpdates a la vez. No es nuestro problema:
                # el otro se está ocupando, y reintentar solo se lo quitaría.
                #
                # Solo ese 409. El otro que existe —"can't use getUpdates
                # method while webhook is active"— es una configuración rota
                # que dejaría las respuestas sin avisar para siempre, así que
                # tiene que verse como el error que es.
                if response.status_code == 409 and "terminated by other" in str(description):
                    raise TelegramConflict(redact(f"{method}: {description}", self._token))

                # 4xx (token malo, chat_id inexistente, HTML mal formado) no se
                # arregla reintentando; 429 y 5xx sí.
                if response.status_code == 429:
                    retry_after = int((body or {}).get("parameters", {}).get("retry_after", 5))
                    _sleep(min(retry_after, 30))
                    continue
                if response.status_code < 500:
                    break

            if attempt < MAX_ATTEMPTS:
                _sleep(2**attempt)

        detalle = redact(f"{method} falló tras {MAX_ATTEMPTS} intento(s) — {last_error}", self._token)
        raise TelegramError(detalle)


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
