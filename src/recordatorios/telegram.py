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
    ) -> dict[str, Any]: ...

    def send_poll(
        self,
        chat_id: str,
        question: str,
        options: Sequence[str],
        silent: bool = False,
    ) -> dict[str, Any]: ...


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
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if silent:
            payload["disable_notification"] = True
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

    def get_me(self) -> dict[str, Any]:
        """Datos del bot. Sirve para comprobar que el token vale."""
        return self._call("getMe", {})

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Datos del chat. Comprueba que el bot lo alcanza, sin mandar nada."""
        return self._call("getChat", {"chat_id": chat_id})

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
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
