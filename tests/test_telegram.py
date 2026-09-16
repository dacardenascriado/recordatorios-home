"""Que el token no se escape a un log.

Va en la URL de cada llamada, y los logs de GitHub Actions son públicos en un
repo público. GitHub enmascara los secrets que reconoce, pero acá no queremos
depender de eso.
"""

from __future__ import annotations

import httpx
import pytest

from recordatorios.telegram import TelegramConflict, TelegramError, TelegramSender, redact

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


def test_get_updates_pide_solo_los_votos_y_no_espera(monkeypatch):
    monkeypatch.setattr(httpx, "Client", _ClienteEspia)

    TelegramSender(TOKEN).get_updates(offset=42, allowed_updates=["poll_answer"])

    payload = _ClienteEspia.ultimo["payload"]
    assert payload["offset"] == 42
    assert payload["allowed_updates"] == ["poll_answer"]
    # timeout 0: esto corre dentro del bucle del tick, no puede quedarse colgado.
    assert payload["timeout"] == 0
    assert _ClienteEspia.ultimo["url"].endswith("/getUpdates")


def test_get_updates_sin_lista_devuelve_vacio(monkeypatch):
    """La Bot API devuelve una lista; ante cualquier otra cosa, no inventamos."""
    monkeypatch.setattr(httpx, "Client", _ClienteEspia)

    assert TelegramSender(TOKEN).get_updates() == []


def test_responder_a_un_mensaje_borrado_no_impide_el_aviso(monkeypatch):
    # Si borraron la encuesta, el aviso tiene que salir igual: es la parte que
    # importa. Sin esto, Telegram devuelve 400 y nadie se entera de nada.
    monkeypatch.setattr(httpx, "Client", _ClienteEspia)

    TelegramSender(TOKEN).send_message("555", "Ana no puede", reply_to_message_id=7)

    payload = _ClienteEspia.ultimo["payload"]
    assert payload["reply_to_message_id"] == 7
    assert payload["allow_sending_without_reply"] is True


class _RespuestaConflicto:
    status_code = 409
    text = ''

    @staticmethod
    def json():
        return {"ok": False, "description": "Conflict: terminated by other getUpdates request"}


def test_dos_lectores_a_la_vez_dan_un_conflicto_reconocible(monkeypatch):
    """Que sea su propia excepción es lo que deja al comando apartarse en
    silencio en vez de teñir la corrida de rojo."""
    intentos = []

    class _Cliente(_ClienteEspia):
        def post(self, url, json):
            intentos.append(url)
            return _RespuestaConflicto()

    monkeypatch.setattr(httpx, "Client", _Cliente)

    with pytest.raises(TelegramConflict):
        TelegramSender(TOKEN).get_updates()

    # Y no se reintenta: el otro lector se está ocupando.
    assert len(intentos) == 1


def test_un_webhook_activo_no_se_confunde_con_el_otro_lector(monkeypatch):
    """El 409 del webhook es una configuración rota: si se tragara en silencio,
    las respuestas no se avisarían nunca y nadie se enteraría."""

    class _RespuestaWebhook:
        status_code = 409
        text = ""

        @staticmethod
        def json():
            return {
                "ok": False,
                "description": "Conflict: can't use getUpdates method while webhook is active",
            }

    class _Cliente(_ClienteEspia):
        def post(self, url, json):
            return _RespuestaWebhook()

    monkeypatch.setattr(httpx, "Client", _Cliente)

    with pytest.raises(TelegramError) as fallo:
        TelegramSender(TOKEN).get_updates()
    assert not isinstance(fallo.value, TelegramConflict)
