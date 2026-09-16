"""Las respuestas de las encuestas: que el grupo se entere, y una sola vez.

Lo que se prueba acá es sobre todo lo que no se ve: que un aviso no salga dos
veces, que uno que no pudo salir se vuelva a intentar, y que el log —público—
no lleve nombres.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recordatorios.models import Reminder
from recordatorios.respuestas import run_respuestas
from recordatorios.store import Store

UTC = timezone.utc
OCURRENCIA = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
AHORA = OCURRENCIA + timedelta(minutes=30)

OPCIONES = (
    "🙅 Hoy no puedo, ¿quién está en la casa para que me haga el favor?",
    "🔄 Ya lo cambié con alguien",
)


class FakeBot:
    """Un bot de mentira: entrega actualizaciones y anota lo que se manda."""

    def __init__(self, updates=(), fallos: int = 0) -> None:
        self.pendientes = list(updates)
        self.enviados: list[tuple[str, str, int | None]] = []
        self.confirmado: int | None = None
        self.pedidos: list = []
        self.fallos = fallos

    def get_updates(self, offset=None, allowed_updates=None, limit=100):
        self.pedidos.append(allowed_updates)
        if offset is not None:
            self.confirmado = offset
            self.pendientes = [u for u in self.pendientes if u["update_id"] >= offset]
            return []
        return list(self.pendientes)

    def send_message(
        self, chat_id, text, parse_mode=None, silent=False, reply_to_message_id=None
    ):
        if self.fallos > 0:
            self.fallos -= 1
            raise RuntimeError("Telegram no responde")
        self.enviados.append((chat_id, text, reply_to_message_id))
        return {"message_id": 900 + len(self.enviados)}

    def send_poll(self, chat_id, question, options, silent=False):  # pragma: no cover
        raise AssertionError("respuestas no manda encuestas")


def voto(update_id: int, poll_id: str = "p1", quien: str = "Ana", opciones=(0,), user_id: int = 7):
    return {
        "update_id": update_id,
        "poll_answer": {
            "poll_id": poll_id,
            "user": {"id": user_id, "first_name": quien},
            "option_ids": list(opciones),
        },
    }


BANO = Reminder(
    id="bano-manana",
    name="Aseo del baño — aviso de la mañana",
    cron="0 5 * * 1,4",
    messages=("🧽 Le toca a {turno}.",),
    chat_id="555",
    poll_options=OPCIONES,
)


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(None, sqlite_path=tmp_path / "test.db")
    s.init_schema()
    s.record_poll(
        poll_id="p1",
        reminder_id="bano-manana",
        occurrence_at=OCURRENCIA,
        chat_id="555",
        message_id=42,
        options=OPCIONES,
        now=OCURRENCIA,
    )
    yield s
    s.close()


def test_un_voto_se_avisa_en_el_chat_colgado_de_la_encuesta(store):
    bot = FakeBot([voto(10)])

    resultado = run_respuestas([BANO], store, bot, now=AHORA)

    assert resultado.avisos == 1
    chat, texto, respondiendo = bot.enviados[0]
    assert chat == "555"
    assert respondiendo == 42  # colgado del mensaje de la encuesta
    assert "Ana" in texto
    assert "Aseo del baño — aviso de la mañana" in texto
    assert OPCIONES[0] in texto
    # Confirmado hasta ese update: Telegram no lo vuelve a mandar.
    assert bot.confirmado == 11


def test_el_mismo_voto_dos_veces_avisa_una_sola(store):
    """Telegram reenvía lo que no se alcanzó a confirmar. El grupo no tiene por
    qué ver el mismo aviso dos veces."""
    run_respuestas([BANO], store, FakeBot([voto(10)]), now=AHORA)

    repetido = FakeBot([voto(10)])
    resultado = run_respuestas([BANO], store, repetido, now=AHORA + timedelta(minutes=5))

    assert repetido.enviados == []
    assert resultado.repetidas == 1
    assert resultado.avisos == 0


def test_cambiar_de_opcion_vuelve_a_avisar(store):
    run_respuestas([BANO], store, FakeBot([voto(10, opciones=(0,))]), now=AHORA)

    bot = FakeBot([voto(11, opciones=(1,))])
    resultado = run_respuestas([BANO], store, bot, now=AHORA + timedelta(minutes=5))

    assert resultado.avisos == 1
    assert OPCIONES[1] in bot.enviados[0][1]


def test_retirar_el_voto_tambien_se_avisa(store):
    """Que alguien se desdiga cambia lo que el resto tiene que hacer."""
    run_respuestas([BANO], store, FakeBot([voto(10)]), now=AHORA)

    bot = FakeBot([voto(11, opciones=())])
    resultado = run_respuestas([BANO], store, bot, now=AHORA + timedelta(minutes=5))

    assert resultado.avisos == 1
    assert "retiró su respuesta" in bot.enviados[0][1]


def test_una_encuesta_que_no_registramos_no_avisa_nada(store):
    """La primera lectura arrastra hasta 24 h de votos viejos. Anunciarlos todos
    de golpe sería un aluvión por cosas que ya pasaron."""
    bot = FakeBot([voto(10, poll_id="de-antes")])

    resultado = run_respuestas([BANO], store, bot, now=AHORA)

    assert bot.enviados == []
    assert resultado.desconocidas == 1
    assert bot.confirmado == 11  # se confirma igual: no tiene que volver


def test_sin_actualizaciones_no_se_abre_la_base(tmp_path: Path):
    bot = FakeBot([])
    with Store.open(None, sqlite_path=tmp_path / "vacia.db") as store:
        resultado = run_respuestas([BANO], store, bot, now=AHORA)

        assert not store.connected
    assert resultado.leidas == 0
    assert resultado.avisos == 0


def test_solo_se_piden_los_votos(store):
    bot = FakeBot([voto(10)])

    run_respuestas([BANO], store, bot, now=AHORA)

    assert bot.pedidos[0] == ["poll_answer"]


def test_un_aviso_que_falla_no_se_confirma_y_se_reintenta(store):
    """Si el mensaje no sale, esa respuesta tiene que volver a llegar. Perderla
    en silencio es exactamente lo que este módulo existe para evitar."""
    fallido = FakeBot([voto(10), voto(11, quien="Bea", user_id=8)], fallos=1)

    resultado = run_respuestas([BANO], store, fallido, now=AHORA)

    assert fallido.enviados == []
    assert fallido.confirmado is None  # nada confirmado: Telegram lo repite
    assert resultado.error is not None

    reintento = FakeBot([voto(10), voto(11, quien="Bea", user_id=8)])
    segunda = run_respuestas([BANO], store, reintento, now=AHORA + timedelta(minutes=5))

    assert segunda.avisos == 2
    assert len(reintento.enviados) == 2
    assert reintento.confirmado == 12


def test_el_informe_no_lleva_nombres(store):
    """El log de Actions es público y los nombres viven en secrets."""
    bot = FakeBot([voto(10, quien="Ana")])

    resultado = run_respuestas([BANO], store, bot, now=AHORA)

    assert "Ana" not in resultado.report()
    assert "bano-manana" in resultado.report()


def test_dry_run_no_manda_ni_registra_ni_confirma(store):
    bot = FakeBot([voto(10)])

    resultado = run_respuestas([BANO], store, bot, now=AHORA, dry_run=True)

    assert bot.enviados == []
    assert bot.confirmado is None
    assert resultado.avisos == 1

    # Y lo que no se avisó sigue pendiente de avisar.
    real = FakeBot([voto(10)])
    assert run_respuestas([BANO], store, real, now=AHORA).avisos == 1


def test_un_voto_anonimo_no_rompe_la_corrida(store):
    """Un canal votando llega con `voter_chat` y sin `user`: no hay a quién
    nombrar, pero tampoco puede frenar los votos que siguen."""
    anonimo = {
        "update_id": 10,
        "poll_answer": {"poll_id": "p1", "voter_chat": {"id": -100}, "option_ids": [0]},
    }
    bot = FakeBot([anonimo, voto(11)])

    resultado = run_respuestas([BANO], store, bot, now=AHORA)

    assert resultado.avisos == 1
    assert bot.confirmado == 12


def test_de_punta_a_punta_la_encuesta_que_salio_y_el_voto_que_llego(tmp_path: Path):
    """La costura entre los dos módulos: el id que el tick guarda al enviar es
    el que `respuestas` usa para saber de qué se trata el voto."""
    from recordatorios.config import Settings
    from recordatorios.tick import run_tick

    class BotCompleto(FakeBot):
        def send_poll(self, chat_id, question, options, silent=False):
            return {"message_id": 77, "poll": {"id": "abc123"}}

    settings = Settings(
        telegram_token="token",
        database_url=None,
        reminders_file=Path("reminders.yaml"),
        lookback_minutes=120,
        max_window_hours=6,
    )
    bot = BotCompleto([voto(10, poll_id="abc123")])

    with Store.open(None, sqlite_path=tmp_path / "e2e.db") as store:
        run_tick([BANO], store, bot, settings, now=AHORA)
        resultado = run_respuestas([BANO], store, bot, now=AHORA + timedelta(minutes=1))

    assert resultado.avisos == 1
    _, texto, respondiendo = bot.enviados[0]
    assert respondiendo == 77  # colgado de la encuesta que acaba de salir
    assert OPCIONES[0] in texto
