"""Que el token no se escape a un log.

Va en la URL de cada llamada, y los logs de GitHub Actions son públicos en un
repo público. GitHub enmascara los secrets que reconoce, pero acá no queremos
depender de eso.
"""

from __future__ import annotations

import httpx

from recordatorios.telegram import TelegramSender, redact

TOKEN = "8870339268:AAF6S7FlAqbcrzFG-mQZSBY0mtELX0uyPk8"


def test_borra_el_token_de_una_url():
    crudo = f"error de red: ConnectTimeout en https://api.telegram.org/bot{TOKEN}/sendMessage"

    limpio = redact(crudo, TOKEN)

    assert TOKEN not in limpio
    assert "https://api.telegram.org/bot***/sendMessage" in limpio


def test_borra_todas_las_apariciones():
    crudo = f"{TOKEN} falló, reintentando con {TOKEN}"
    assert redact(crudo, TOKEN).count("***") == 2


def test_deja_intacto_lo_que_no_es_el_token():
    crudo = "HTTP 400: chat not found"
    assert redact(crudo, TOKEN) == crudo


def test_sin_token_no_rompe():
    assert redact("algo", None) == "algo"
    assert redact("algo", "") == "algo"


class _RespuestaOk:
    status_code = 200

    @staticmethod
    def json():
        return {"ok": True, "result": {"message_id": 1}}


class _ClienteEspia:
    """Captura el payload que se le manda a la Bot API."""

    ultimo: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def post(self, url, json):
        type(self).ultimo = {"url": url, "payload": json}
        return _RespuestaOk()


def test_send_poll_manda_las_opciones_como_input_poll_option(monkeypatch):
    # La Bot API documenta InputPollOption desde la 7.3. Si esto se mandara como
    # lista de textos y Telegram dejara de aceptarla, el síntoma sería un
    # recordatorio que no llega — el fallo que menos se nota.
    monkeypatch.setattr(httpx, "Client", _ClienteEspia)

    TelegramSender(TOKEN).send_poll("555", "¿te encargas?", ["Sí", "No"])

    payload = _ClienteEspia.ultimo["payload"]
    assert payload["options"] == [{"text": "Sí"}, {"text": "No"}]
    assert payload["question"] == "¿te encargas?"
    assert payload["chat_id"] == "555"
    # Anónima no serviría: lo que se quiere saber es si contestó quien le toca.
    assert payload["is_anonymous"] is False
    assert _ClienteEspia.ultimo["url"].endswith("/sendPoll")


def test_send_poll_silencioso_no_notifica(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ClienteEspia)

    TelegramSender(TOKEN).send_poll("555", "¿te encargas?", ["Sí", "No"], silent=True)

    assert _ClienteEspia.ultimo["payload"]["disable_notification"] is True
