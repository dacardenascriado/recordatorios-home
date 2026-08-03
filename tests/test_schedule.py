"""El corazón del asunto: que los lunes alternos se turnen de verdad."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from recordatorios.models import Reminder
from recordatorios.schedule import (
    last_run,
    monday_of,
    nearest_run,
    next_runs,
    occurrences_between,
    week_matches,
)

BOGOTA = "America/Bogota"
UTC = timezone.utc

# 2026-08-03 es lunes.
LUNES_ANCHOR = date(2026, 8, 3)


def make(**overrides) -> Reminder:
    base = dict(
        id="r",
        name="r",
        cron="0 7 * * 1",
        messages=("hola",),
        chat_id="1",
        timezone=BOGOTA,
    )
    base.update(overrides)
    return Reminder(**base)


def test_lunes_alternos_se_turnan_sin_solaparse():
    a = make(id="a", every_weeks=2, week_offset=0, anchor=LUNES_ANCHOR)
    b = make(id="b", every_weeks=2, week_offset=1, anchor=LUNES_ANCHOR)
    todos = make(id="todos")

    inicio = datetime(2026, 8, 1, tzinfo=UTC)
    fin = inicio + timedelta(days=70)

    occ_a = occurrences_between(a, inicio, fin)
    occ_b = occurrences_between(b, inicio, fin)
    occ_todos = occurrences_between(todos, inicio, fin)

    # Ninguno pisa al otro...
    assert set(occ_a).isdisjoint(occ_b)
    # ...y entre los dos cubren todos los lunes, sin dejar ninguno afuera.
    assert sorted(occ_a + occ_b) == occ_todos
    # Se turnan estrictamente: a, b, a, b, ...
    assert occ_a[0] < occ_b[0] < occ_a[1] < occ_b[1]
    # Cada uno cae cada 14 días (Bogotá no tiene horario de verano).
    assert all(y - x == timedelta(days=14) for x, y in zip(occ_a, occ_a[1:]))


def test_el_offset_decide_cual_arranca_primero():
    a = make(id="a", every_weeks=2, week_offset=0, anchor=LUNES_ANCHOR)
    b = make(id="b", every_weeks=2, week_offset=1, anchor=LUNES_ANCHOR)
    desde = datetime(2026, 8, 1, tzinfo=UTC)

    # El anchor (semana 0) le corresponde al offset 0.
    assert next_runs(a, 1, desde)[0].astimezone(ZoneInfo(BOGOTA)).date() == date(2026, 8, 3)
    assert next_runs(b, 1, desde)[0].astimezone(ZoneInfo(BOGOTA)).date() == date(2026, 8, 10)


def test_anchor_se_normaliza_al_lunes_de_su_semana():
    miercoles = date(2026, 8, 5)
    assert monday_of(miercoles) == LUNES_ANCHOR

    con_lunes = make(every_weeks=2, week_offset=0, anchor=LUNES_ANCHOR)
    con_miercoles = make(every_weeks=2, week_offset=0, anchor=miercoles)
    inicio = datetime(2026, 8, 1, tzinfo=UTC)
    fin = inicio + timedelta(days=42)

    assert occurrences_between(con_lunes, inicio, fin) == occurrences_between(con_miercoles, inicio, fin)


def test_every_weeks_de_tres_reparte_las_semanas_en_tres_grupos():
    grupos = [
        make(id=f"g{i}", every_weeks=3, week_offset=i, anchor=LUNES_ANCHOR) for i in range(3)
    ]
    inicio = datetime(2026, 8, 1, tzinfo=UTC)
    fin = inicio + timedelta(days=63)

    ocurrencias = [occurrences_between(g, inicio, fin) for g in grupos]
    todos = occurrences_between(make(id="todos"), inicio, fin)

    assert sorted(sum(ocurrencias, [])) == todos
    for i, unos in enumerate(ocurrencias):
        for otros in ocurrencias[i + 1 :]:
            assert set(unos).isdisjoint(otros)


def test_week_matches_cuenta_semanas_completas_desde_el_anchor():
    r = make(every_weeks=2, week_offset=0, anchor=LUNES_ANCHOR)
    assert week_matches(r, date(2026, 8, 3)) is True  # semana 0
    assert week_matches(r, date(2026, 8, 9)) is True  # domingo, misma semana 0
    assert week_matches(r, date(2026, 8, 10)) is False  # semana 1
    assert week_matches(r, date(2026, 8, 17)) is True  # semana 2
    assert week_matches(r, date(2026, 7, 27)) is False  # semana -1


def test_sin_every_weeks_dispara_todas_las_semanas():
    r = make()
    assert all(week_matches(r, date(2026, 8, 3) + timedelta(weeks=w)) for w in range(6))


def test_ventana_con_inicio_exclusivo_y_fin_inclusivo():
    # Ticks consecutivos deben cubrir la línea de tiempo sin huecos ni repetidos.
    r = make(cron="0 7 * * *")
    momento = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)  # 07:00 en Bogotá (UTC-5)

    assert occurrences_between(r, momento, momento + timedelta(hours=1)) == []
    assert occurrences_between(r, momento - timedelta(seconds=1), momento) == [momento]


def test_starts_on_y_ends_on_recortan_el_rango():
    r = make(starts_on=date(2026, 8, 10), ends_on=date(2026, 8, 24))
    inicio = datetime(2026, 8, 1, tzinfo=UTC)
    fin = inicio + timedelta(days=60)

    fechas = [o.astimezone(ZoneInfo(BOGOTA)).date() for o in occurrences_between(r, inicio, fin)]
    assert fechas == [date(2026, 8, 10), date(2026, 8, 17), date(2026, 8, 24)]


def test_la_hora_local_no_se_mueve_con_el_cambio_de_horario():
    # En una zona con horario de verano, "las 9" siguen siendo las 9 locales
    # aunque el instante UTC cambie.
    madrid = ZoneInfo("Europe/Madrid")
    r = make(cron="0 9 * * *", timezone="Europe/Madrid")
    inicio = datetime(2026, 10, 20, tzinfo=UTC)

    ocurrencias = occurrences_between(r, inicio, inicio + timedelta(days=14))
    assert len(ocurrencias) == 14
    assert {o.astimezone(madrid).hour for o in ocurrencias} == {9}


def test_recordatorio_ya_vencido_no_tiene_proximas_ejecuciones():
    r = make(ends_on=date(2020, 1, 1))
    assert next_runs(r, 3, datetime(2026, 8, 1, tzinfo=UTC)) == []


# -- mirar hacia atrás -----------------------------------------------------

# Lunes 3 de agosto de 2026, 07:00 en Bogotá (UTC-5).
LUNES_7AM = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
# Ese mismo lunes a las 16:34, la hora a la que se descubrió el olvido.
LUNES_TARDE = datetime(2026, 8, 3, 21, 34, tzinfo=UTC)


def test_last_run_devuelve_la_ocurrencia_anterior():
    assert last_run(make(cron="0 7 * * 1,3"), LUNES_TARDE) == LUNES_7AM


def test_last_run_respeta_el_filtro_de_semanas():
    # Semanas alternas: antes del 14, el último lunes que le tocó es el 3, no
    # el 10, que le corresponde al otro offset.
    r = make(every_weeks=2, week_offset=0, anchor=LUNES_ANCHOR)
    assert last_run(r, datetime(2026, 8, 14, tzinfo=UTC)) == LUNES_7AM


def test_last_run_no_se_va_antes_de_starts_on():
    r = make(starts_on=date(2026, 8, 10))
    assert last_run(r, datetime(2026, 8, 5, tzinfo=UTC)) is None


def test_nearest_run_prefiere_la_de_hoy_si_ya_disparo():
    # El caso que motivó todo esto: una prueba el lunes por la tarde tiene que
    # imitar la ocurrencia de las 7:00 de hoy, no la del miércoles.
    assert nearest_run(make(cron="0 7 * * 1,3"), LUNES_TARDE) == LUNES_7AM


def test_nearest_run_mira_adelante_cuando_lo_que_viene_esta_mas_cerca():
    r = make(cron="0 7 * * 1,3")
    # Martes a las 16:00: el miércoles queda a ~15 h y el lunes a ~33 h.
    martes = datetime(2026, 8, 4, 21, 0, tzinfo=UTC)
    assert nearest_run(r, martes) == datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_nearest_run_sin_pasado_devuelve_la_proxima():
    r = make(starts_on=date(2026, 8, 10))
    esperada = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    assert nearest_run(r, datetime(2026, 8, 5, tzinfo=UTC)) == esperada


def test_nearest_run_sin_futuro_devuelve_la_ultima():
    r = make(ends_on=date(2026, 8, 3))
    assert nearest_run(r, datetime(2026, 8, 20, tzinfo=UTC)) == LUNES_7AM
