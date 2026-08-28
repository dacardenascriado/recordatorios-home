"""El tick: recuperar retrasos, no enviar nada dos veces, y no despertar la
base de datos cuando no hay nada que hacer."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recordatorios.config import DEFAULT_LOOKBACK_MINUTES, Settings
from recordatorios.models import DEFAULT_MAX_DELAY_MINUTES, Reminder
from recordatorios.store import Store
from recordatorios.tick import run_tick

UTC = timezone.utc

# 2026-08-03 07:00 en Bogotá (UTC-5).
OCURRENCIA = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class FakeSender:
    """Emisor de mentira: cuenta envíos y puede fallar a demanda."""

    def __init__(self, fallos: int = 0) -> None:
        self.enviados: list[tuple[str, str]] = []
        self.fallos_restantes = fallos

    def send_message(self, chat_id, text, parse_mode=None, silent=False):
        if self.fallos_restantes > 0:
            self.fallos_restantes -= 1
            raise RuntimeError("Telegram no responde")
        self.enviados.append((chat_id, text))
        return {"message_id": len(self.enviados)}


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(None, sqlite_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_token="token",
        database_url=None,
        reminders_file=Path("reminders.yaml"),
        lookback_minutes=120,
        max_window_hours=6,
    )


def lunes(**overrides) -> Reminder:
    base = dict(
        id="lunes",
        name="Lunes",
        cron="0 7 * * 1",
        messages=("sacar la basura",),
        chat_id="555",
        timezone="America/Bogota",
    )
    base.update(overrides)
    return Reminder(**base)


def test_envia_lo_que_vencio_en_la_ventana(store, settings):
    sender = FakeSender()

    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=3))

    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert sender.enviados == [("555", "sacar la basura")]


def test_no_envia_dos_veces_la_misma_ocurrencia(store, settings):
    sender = FakeSender()
    run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=3))

    # El tick siguiente vuelve a ver la misma ocurrencia dentro de su ventana.
    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=8))

    assert [o.status for o in resultado.outcomes] == ["already_handled"]
    assert len(sender.enviados) == 1


def test_recupera_un_tick_atrasado(store, settings):
    # El workflow no corrió por un buen rato; la ocurrencia cayó en el medio.
    sender = FakeSender()

    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=50))

    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert len(sender.enviados) == 1


def test_la_ventana_por_defecto_es_mas_ancha_que_el_plazo():
    # La invariante que hace visible lo vencido. Si fueran iguales, cualquier
    # ocurrencia pasada de plazo quedaría también fuera de la ventana: el tick
    # no la vería nunca y el recordatorio se perdería en silencio.
    assert DEFAULT_LOOKBACK_MINUTES > DEFAULT_MAX_DELAY_MINUTES


def test_descarta_lo_que_llega_demasiado_tarde(store, settings):
    # Con un max_delay más corto que la ventana, la ocurrencia entra en el
    # rango pero igual se descarta por vieja.
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1))

    assert [o.status for o in resultado.outcomes] == ["skipped_stale"]
    # El recordatorio en sí no sale; lo único que va al chat es el aviso de que
    # se perdió.
    assert "sacar la basura" not in [texto for _, texto in sender.enviados]
    # Descartar sí toca la base: perder un recordatorio sin dejar rastro es el
    # peor modo de fallo, así que queda anotado.
    assert resultado.touched_database is True
    assert [f.status for f in store.history()] == ["stale"]


def test_lo_descartado_se_anota_una_sola_vez(store, settings):
    # La ocurrencia vencida sigue apareciendo en la ventana durante horas: sin
    # esto, cada tick agregaría una fila y mantendría a Neon despierta.
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)
    run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1))

    resultado = run_tick(
        [reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1, minutes=5)
    )

    assert [o.status for o in resultado.outcomes] == ["already_handled"]
    assert len(store.history()) == 1


def test_lo_ya_enviado_no_se_marca_despues_como_vencido(store, settings):
    # Se envió a tiempo, pero la ocurrencia sigue en la ventana hasta pasarse
    # de plazo. Un 'sent' es definitivo y no lo pisa nadie.
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)
    run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=45))

    assert [o.status for o in resultado.outcomes] == ["already_handled"]
    assert [f.status for f in store.history()] == ["sent"]
    assert len(sender.enviados) == 1


def test_un_fallo_se_reintenta_en_el_siguiente_tick(store, settings):
    sender = FakeSender(fallos=1)

    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))
    assert [o.status for o in resultado.outcomes] == ["failed"]
    assert sender.enviados == []

    # La ocurrencia sigue dentro de la ventana, así que se reintenta sola.
    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=7))
    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert len(sender.enviados) == 1


def test_un_fallo_deja_de_reintentarse_al_pasarse_de_max_delay(store, settings):
    sender = FakeSender(fallos=99)
    reminder = lunes(max_delay_minutes=30)

    run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=5))
    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=45))

    assert [o.status for o in resultado.outcomes] == ["skipped_stale"]
    # El intento fallido termina de vencerse y queda registrado como tal.
    assert [f.status for f in store.history()] == ["stale", "failed"]


def test_un_fallo_no_bloquea_a_los_demas(store, settings):
    class SoloFallaUno(FakeSender):
        def send_message(self, chat_id, text, parse_mode=None, silent=False):
            if chat_id == "malo":
                raise RuntimeError("chat inexistente")
            return super().send_message(chat_id, text, parse_mode, silent)

    sender = SoloFallaUno()
    recordatorios = [lunes(id="malo", chat_id="malo"), lunes(id="bueno")]

    resultado = run_tick(
        recordatorios, store, sender, settings, now=OCURRENCIA + timedelta(minutes=2)
    )

    assert {o.reminder.id: o.status for o in resultado.outcomes} == {
        "malo": "failed",
        "bueno": "sent",
    }
    assert len(sender.enviados) == 1


def test_ignora_los_pausados(store, settings):
    sender = FakeSender()

    resultado = run_tick(
        [lunes(enabled=False)], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2)
    )

    assert resultado.outcomes == []
    assert sender.enviados == []


def test_un_tick_sin_nada_pendiente_no_abre_la_conexion(store, settings):
    # Esta es la propiedad que mantiene el compute de Neon dormido: de los ~8600
    # ticks del mes, casi todos terminan acá.
    sender = FakeSender()
    martes = OCURRENCIA + timedelta(days=1)

    resultado = run_tick([lunes()], store, sender, settings, now=martes)

    assert resultado.outcomes == []
    assert resultado.touched_database is False
    assert store.connected is False


def test_dry_run_no_envia_ni_escribe(store, settings):
    sender = FakeSender()
    ahora = OCURRENCIA + timedelta(minutes=2)

    resultado = run_tick([lunes()], store, sender, settings, now=ahora, dry_run=True)

    assert [o.status for o in resultado.outcomes] == ["would_send"]
    assert sender.enviados == []
    assert store.connected is False

    # Y después de simular, el envío real sigue disponible.
    resultado = run_tick([lunes()], store, sender, settings, now=ahora)
    assert [o.status for o in resultado.outcomes] == ["sent"]


def test_la_ventana_no_pasa_del_tope_duro(store, settings):
    holgada = Settings(
        telegram_token="token",
        database_url=None,
        reminders_file=Path("reminders.yaml"),
        lookback_minutes=60 * 24 * 10,  # diez días, a todas luces demasiado
        max_window_hours=6,
    )
    sender = FakeSender()
    ahora = OCURRENCIA + timedelta(minutes=2)

    resultado = run_tick([lunes()], store, sender, holgada, now=ahora)

    assert resultado.window_start == ahora - timedelta(hours=6)


def test_los_lunes_alternos_no_se_pisan(store, settings):
    from datetime import date

    anchor = date(2026, 8, 3)
    a = lunes(id="a", every_weeks=2, week_offset=0, anchor=anchor, messages=("A",))
    b = lunes(id="b", every_weeks=2, week_offset=1, anchor=anchor, messages=("B",))
    sender = FakeSender()

    for semana, esperado in enumerate(["A", "B", "A", "B"]):
        momento = OCURRENCIA + timedelta(days=7 * semana, minutes=1)
        run_tick([a, b], store, sender, settings, now=momento)
        assert [texto for _, texto in sender.enviados][-1] == esperado

    assert [texto for _, texto in sender.enviados] == ["A", "B", "A", "B"]


def test_el_cache_evita_volver_a_preguntarle_a_la_base(store, settings, tmp_path):
    # La ocurrencia enviada sigue en la ventana durante horas. Sin caché, cada
    # tick de ese rato abre la conexión solo para confirmar lo que ya sabe, y
    # con una ventana de 12 h eso deja el compute de Neon despierto casi 24/7.
    con_cache = replace(settings, state_file=tmp_path / "tick-state.json")
    sender = FakeSender()
    run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=2))

    resultado = run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=7))

    assert [o.status for o in resultado.outcomes] == ["already_handled"]
    assert resultado.touched_database is False
    assert len(sender.enviados) == 1


def test_un_fallo_no_entra_al_cache(store, settings, tmp_path):
    # Si un fallo se cacheara, el reintento no ocurriría nunca: el caché estaría
    # convirtiendo un envío recuperable en una pérdida.
    con_cache = replace(settings, state_file=tmp_path / "tick-state.json")
    sender = FakeSender(fallos=1)
    run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=2))

    resultado = run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=7))

    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert len(sender.enviados) == 1


def test_un_cache_ilegible_no_rompe_el_tick(store, settings, tmp_path):
    # El caché es un atajo de costo, no una fuente de verdad: si no se puede
    # leer, se preguntará a la base y ya.
    estado = tmp_path / "tick-state.json"
    estado.write_text("{esto no es json", encoding="utf-8")
    con_cache = replace(settings, state_file=estado)
    sender = FakeSender()

    resultado = run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=2))

    assert [o.status for o in resultado.outcomes] == ["sent"]


def test_el_dry_run_no_toca_el_cache(store, settings, tmp_path):
    estado = tmp_path / "tick-state.json"
    con_cache = replace(settings, state_file=estado)
    sender = FakeSender()

    run_tick([lunes()], store, sender, con_cache, now=OCURRENCIA + timedelta(minutes=2), dry_run=True)

    assert estado.exists() is False


def test_una_perdida_se_avisa_por_telegram(store, settings):
    # Sin este aviso, una pérdida solo existe en un log de Actions que nadie
    # mira. Es lo que hizo que una caída de dos días se notara recién cuando
    # alguien echó de menos un mensaje.
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1))

    assert resultado.alerts_sent == 1
    assert resultado.alert_error is None
    chat, texto = sender.enviados[-1]
    assert chat == "555"
    assert "no salió a tiempo" in texto
    assert reminder.name in texto


def test_la_perdida_se_avisa_una_sola_vez(store, settings):
    # La ocurrencia vencida sigue en la ventana durante horas. Avisar en cada
    # tick sería peor que no avisar: el grupo aprendería a ignorar el aviso.
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)
    run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1))

    resultado = run_tick(
        [reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1, minutes=5)
    )

    assert resultado.alerts_sent == 0
    assert len(sender.enviados) == 1


def test_un_envio_normal_no_dispara_aviso(store, settings):
    sender = FakeSender()

    resultado = run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=3))

    assert resultado.alerts_sent == 0
    assert [texto for _, texto in sender.enviados] == ["sacar la basura"]


def test_si_el_aviso_falla_el_tick_igual_termina(store, settings):
    # Avisar es un extra. Que Telegram esté caído no puede convertir un tick
    # que hizo su trabajo en un tick que revienta.
    sender = FakeSender(fallos=99)
    reminder = lunes(max_delay_minutes=30)

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1))

    assert [o.status for o in resultado.outcomes] == ["skipped_stale"]
    assert resultado.alerts_sent == 0
    assert "Telegram no responde" in resultado.alert_error
    assert [f.status for f in store.history()] == ["stale"]


def test_el_dry_run_no_avisa(store, settings):
    sender = FakeSender()
    reminder = lunes(max_delay_minutes=30)

    resultado = run_tick(
        [reminder], store, sender, settings, now=OCURRENCIA + timedelta(hours=1), dry_run=True
    )

    assert [o.status for o in resultado.outcomes] == ["skipped_stale"]
    assert resultado.alerts_sent == 0
    assert sender.enviados == []


def test_el_historial_registra_los_envios(store, settings):
    sender = FakeSender()
    run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))

    historial = store.history()
    assert len(historial) == 1
    assert historial[0].reminder_id == "lunes"
    assert historial[0].status == "sent"
    assert historial[0].occurrence_at == OCURRENCIA


class FakePollSender(FakeSender):
    """Además de mensajes, registra encuestas."""

    def __init__(self, fallos: int = 0) -> None:
        super().__init__(fallos)
        self.encuestas: list[tuple[str, str, tuple[str, ...]]] = []

    def send_poll(self, chat_id, question, options, silent=False):
        if self.fallos_restantes > 0:
            self.fallos_restantes -= 1
            raise RuntimeError("Telegram no responde")
        self.encuestas.append((chat_id, question, tuple(options)))
        return {"message_id": len(self.encuestas)}


def test_un_recordatorio_con_poll_se_manda_como_encuesta(store, settings):
    sender = FakePollSender()
    reminder = lunes(
        messages=("<b>{turno}</b>, ¿sacas la basura?",),
        rotation=("Ana",),
        poll_options=("Sí", "No"),
    )

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))

    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert sender.enviados == []
    chat, pregunta, opciones = sender.encuestas[0]
    assert chat == "555"
    # Las encuestas no interpretan HTML: la pregunta va sin etiquetas.
    assert pregunta == "Ana, ¿sacas la basura?"
    assert opciones == ("Sí", "No")


def test_sin_poll_se_sigue_mandando_como_mensaje(store, settings):
    sender = FakePollSender()

    run_tick([lunes()], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))

    assert sender.encuestas == []
    assert sender.enviados == [("555", "sacar la basura")]


def test_una_encuesta_que_falla_se_reintenta_como_cualquier_envio(store, settings):
    sender = FakePollSender(fallos=1)
    reminder = lunes(poll_options=("Sí", "No"))

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=2))
    assert [o.status for o in resultado.outcomes] == ["failed"]

    resultado = run_tick([reminder], store, sender, settings, now=OCURRENCIA + timedelta(minutes=7))
    assert [o.status for o in resultado.outcomes] == ["sent"]
    assert len(sender.encuestas) == 1
