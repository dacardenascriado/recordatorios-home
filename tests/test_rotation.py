"""Turnos: quién limpia el baño y qué día.

El caso de referencia es el que pidió el usuario: lunes y jueves, tres
personas, y el turno avanza por día de aseo (no por semana), así que el ciclo
completo dura tres semanas.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from recordatorios.config import ConfigError
from recordatorios.loader import load_reminders
from recordatorios.models import Reminder
from recordatorios.schedule import occurrences_between, turn_index

BOGOTA = ZoneInfo("America/Bogota")
UTC = timezone.utc
ANCHOR = date(2026, 8, 3)  # lunes
EQUIPO = ("Ana", "Beto", "Carla")


def aseo(**overrides) -> Reminder:
    base = dict(
        id="bano",
        name="Baño",
        cron="0 6 * * 1,4",
        messages=("Hoy le toca a {turno}",),
        chat_id="1",
        timezone="America/Bogota",
        rotation=EQUIPO,
        anchor=ANCHOR,
    )
    base.update(overrides)
    return Reminder(**base)


def asignaciones(reminder: Reminder, dias: int = 28) -> list[tuple[date, str]]:
    """(fecha local, a quién le toca) para las próximas ocurrencias."""
    desde = datetime(2026, 8, 1, tzinfo=UTC)
    salida = []
    for occurrence in occurrences_between(reminder, desde, desde + timedelta(days=dias)):
        turno = turn_index(reminder, occurrence)
        salida.append((occurrence.astimezone(BOGOTA).date(), reminder.whose_turn(turno)))
    return salida


def test_el_turno_sigue_la_secuencia_pedida():
    assert asignaciones(aseo(), dias=21) == [
        (date(2026, 8, 3), "Ana"),  # lunes  semana 1
        (date(2026, 8, 6), "Beto"),  # jueves semana 1
        (date(2026, 8, 10), "Carla"),  # lunes  semana 2
        (date(2026, 8, 13), "Ana"),  # jueves semana 2
        (date(2026, 8, 17), "Beto"),  # lunes  semana 3
        (date(2026, 8, 20), "Carla"),  # jueves semana 3
    ]


def test_el_ciclo_se_repite_cada_tres_semanas():
    ciclo = asignaciones(aseo(), dias=42)
    primeras_seis = [quien for _, quien in ciclo[:6]]
    siguientes_seis = [quien for _, quien in ciclo[6:12]]
    assert primeras_seis == siguientes_seis


def test_todos_hacen_la_misma_cantidad_de_turnos():
    # Sobre un ciclo completo (6 turnos) nadie hace más que otro.
    quienes = [quien for _, quien in asignaciones(aseo(), dias=21)]
    assert sorted(quienes) == sorted(EQUIPO * 2)


def test_los_dos_avisos_del_mismo_dia_le_tocan_a_la_misma_persona():
    # Esta es la razón de que el turno cuente días y no ocurrencias: si contara
    # ocurrencias, a las 6pm le tocaría la persona siguiente.
    manana = aseo(id="manana", cron="0 6 * * 1,4")
    tarde = aseo(id="tarde", cron="0 18 * * 1,4")

    assert asignaciones(manana) == asignaciones(tarde)


def test_un_cron_con_dos_horas_en_el_mismo_dia_no_adelanta_el_turno():
    doble = aseo(cron="0 6,18 * * 1,4")
    porfecha: dict[date, set[str]] = {}
    for fecha, quien in asignaciones(doble):
        porfecha.setdefault(fecha, set()).add(quien)

    assert all(len(quienes) == 1 for quienes in porfecha.values())


def test_antes_del_anchor_el_turno_es_el_primero():
    reminder = aseo()
    antes = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
    assert turn_index(reminder, antes) == 0


def test_los_mensajes_se_van_turnando():
    reminder = aseo(messages=("A {turno}", "B {turno}", "C {turno}"))

    assert reminder.render(0) == "A Ana"
    assert reminder.render(1) == "B Beto"
    assert reminder.render(2) == "C Carla"
    assert reminder.render(3) == "A Ana"


def test_listas_de_distinto_largo_tardan_en_repetir_la_combinacion():
    # 3 personas x 5 mensajes = 15 turnos antes de repetir el mismo par.
    reminder = aseo(messages=tuple(f"M{i} {{turno}}" for i in range(5)))
    combinaciones = {reminder.render(t) for t in range(15)}
    assert len(combinaciones) == 15
    assert reminder.render(15) == reminder.render(0)


def test_siguiente_nombra_a_quien_viene_despues():
    reminder = aseo(messages=("Hoy {turno}, mañana {siguiente}",))

    assert reminder.render(0) == "Hoy Ana, mañana Beto"
    assert reminder.render(2) == "Hoy Carla, mañana Ana"  # da la vuelta


def test_sin_rotation_el_mensaje_sale_tal_cual():
    reminder = aseo(rotation=(), messages=("Sin turnos {turno}",))
    # Sin lista de turnos no hay a quién nombrar: el marcador queda intacto.
    assert reminder.render(0) == "Sin turnos {turno}"
    assert reminder.whose_turn(0) is None


def test_needs_turn_evita_el_calculo_cuando_no_hace_falta():
    assert aseo().needs_turn is True
    assert aseo(rotation=(), messages=("uno", "dos")).needs_turn is True
    assert aseo(rotation=(), messages=("solo uno",)).needs_turn is False


# -- el reparto real de la basura ------------------------------------------


def test_la_basura_reparte_lunes_y_miercoles_entre_los_tres():
    # Dora va aparte (viernes fijo), así que estos tres se turnan 2 días por
    # semana: ciclo de 3 semanas y a cada uno le toca un lunes y un miércoles.
    basura = aseo(
        id="basura-lun-mie",
        cron="0 7 * * 1,3",
        rotation=("Carla", "Beto", "Ana"),
    )

    assert asignaciones(basura, dias=21) == [
        (date(2026, 8, 3), "Carla"),  # lunes     semana 1
        (date(2026, 8, 5), "Beto"),  # miércoles semana 1
        (date(2026, 8, 10), "Ana"),  # lunes     semana 2
        (date(2026, 8, 12), "Carla"),  # miércoles semana 2
        (date(2026, 8, 17), "Beto"),  # lunes     semana 3
        (date(2026, 8, 19), "Ana"),  # miércoles semana 3
    ]


def test_en_la_basura_nadie_queda_pegado_al_mismo_dia():
    # Esta es la degeneración que sí aparecería con 3 personas y 3 días: cada
    # uno caería siempre en el mismo día. Con 2 días por semana no pasa.
    basura = aseo(
        id="basura-lun-mie",
        cron="0 7 * * 1,3",
        rotation=("Carla", "Beto", "Ana"),
    )

    dias_por_persona: dict[str, set[int]] = {}
    for fecha, quien in asignaciones(basura, dias=21):
        dias_por_persona.setdefault(quien, set()).add(fecha.weekday())

    # weekday(): 0 = lunes, 2 = miércoles. Todos hacen los dos.
    assert dias_por_persona == {
        "Carla": {0, 2},
        "Beto": {0, 2},
        "Ana": {0, 2},
    }


# Los nombres reales viven en secrets de GitHub, no en el repo. Acá se cargan
# con relleno, igual que hace el CI.
ENV_REPO = {
    "TELEGRAM_CHAT_ID": "0",
    "PERSONA_1": "Persona-1",
    "PERSONA_2": "Persona-2",
    "PERSONA_3": "Persona-3",
    "PERSONA_4": "Persona-4",
}


def repo_reminders() -> dict[str, Reminder]:
    raiz = Path(__file__).resolve().parents[1]
    return {r.id: r for r in load_reminders(raiz / "reminders.yaml", env=ENV_REPO)}


def test_ningun_nombre_real_quedo_escrito_en_el_repo():
    # El repo es público: los nombres tienen que entrar por ${PERSONA_n}.
    raiz = Path(__file__).resolve().parents[1]
    crudo = (raiz / "reminders.yaml").read_text(encoding="utf-8")

    for reminder in repo_reminders().values():
        for nombre in reminder.rotation:
            # Lo que quedó en el objeto salió del entorno, no del archivo.
            assert nombre in ENV_REPO.values()
            assert nombre not in crudo


def test_el_viernes_de_la_basura_es_siempre_de_la_misma_persona():
    por_id = repo_reminders()
    fija = ENV_REPO["PERSONA_4"]

    for viernes in (por_id["basura-viernes-manana"], por_id["basura-viernes-tarde"]):
        # No rota: no hay lista de turnos y el nombre va en cada texto.
        assert viernes.rotation == ()
        assert all(fija in texto for texto in viernes.messages)
        assert viernes.cron.split()[-1] == "5"

    # Y esa persona no aparece en los otros días.
    for lun_mie in (por_id["basura-lun-mie-manana"], por_id["basura-lun-mie-tarde"]):
        assert fija not in lun_mie.rotation
        assert lun_mie.cron.split()[-1] == "1,3"


@pytest.mark.parametrize(
    ("manana", "tarde"),
    [
        ("bano-manana", "bano-tarde"),
        ("basura-lun-mie-manana", "basura-lun-mie-tarde"),
        ("basura-viernes-manana", "basura-viernes-tarde"),
    ],
)
def test_el_aviso_y_el_control_del_mismo_dia_nombran_a_la_misma_persona(manana, tarde):
    # Si divergieran en rotation, anchor o días de cron, cada uno contaría su
    # propia secuencia y a la tarde le tocaría otra persona.
    por_id = repo_reminders()
    a, b = por_id[manana], por_id[tarde]

    assert a.rotation == b.rotation
    assert a.anchor == b.anchor
    assert a.cron.split()[-1] == b.cron.split()[-1]
    # 20 días y no un múltiplo de 7: así el fin de la ventana no cae justo
    # sobre una ocurrencia de las 19:00, que en UTC es medianoche del día
    # siguiente y haría que las dos listas terminen en puntos distintos.
    assert asignaciones(a, dias=20) == asignaciones(b, dias=20)


def test_el_control_de_la_tarde_llega_despues_del_aviso():
    por_id = repo_reminders()
    for manana, tarde in [
        ("bano-manana", "bano-tarde"),
        ("basura-lun-mie-manana", "basura-lun-mie-tarde"),
        ("basura-viernes-manana", "basura-viernes-tarde"),
    ]:
        hora_aviso = int(por_id[manana].cron.split()[1])
        hora_control = int(por_id[tarde].cron.split()[1])
        assert hora_aviso < hora_control


# -- validación del YAML ---------------------------------------------------


def escribir(tmp_path: Path, cuerpo: str) -> Path:
    ruta = tmp_path / "reminders.yaml"
    ruta.write_text(cuerpo, encoding="utf-8")
    return ruta


BASE = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: bano
    cron: "0 6 * * 1,4"
{extra}
"""


def cargar(tmp_path: Path, extra: str):
    return load_reminders(escribir(tmp_path, BASE.format(extra=extra)), env={})


def test_rotation_exige_anchor(tmp_path):
    with pytest.raises(ConfigError) as exc:
        cargar(
            tmp_path,
            '    rotation: [Ana, Beto]\n    message: "le toca a {turno}"',
        )
    assert any("hace falta 'anchor'" in p for p in exc.value.problems)


def test_varios_mensajes_exigen_anchor(tmp_path):
    with pytest.raises(ConfigError) as exc:
        cargar(tmp_path, '    message:\n      - "uno"\n      - "dos"')
    assert any("hace falta 'anchor'" in p for p in exc.value.problems)


def test_rotation_sin_marcador_de_turno_es_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        cargar(
            tmp_path,
            "    rotation: [Ana, Beto]\n"
            "    anchor: 2026-08-03\n"
            '    message: "limpiar el baño"',
        )
    assert any("ningún mensaje usa {turno}" in p for p in exc.value.problems)


def test_rotation_de_una_sola_persona_es_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        cargar(
            tmp_path,
            "    rotation: [Ana]\n"
            "    anchor: 2026-08-03\n"
            '    message: "le toca a {turno}"',
        )
    assert any("al menos 2 nombres" in p for p in exc.value.problems)


def test_un_mensaje_solo_sigue_siendo_valido(tmp_path):
    recordatorios = cargar(tmp_path, '    message: "hola"')
    assert recordatorios[0].messages == ("hola",)
    assert recordatorios[0].rotation == ()


def test_lista_de_mensajes_con_anchor_es_valida(tmp_path):
    recordatorios = cargar(
        tmp_path, '    anchor: 2026-08-03\n    message:\n      - "uno"\n      - "dos"'
    )
    assert recordatorios[0].messages == ("uno", "dos")
