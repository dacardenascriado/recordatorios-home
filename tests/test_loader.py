"""Validación de reminders.yaml: los errores se reportan todos juntos."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from recordatorios.config import ConfigError
from recordatorios.loader import load_reminders

VALIDO = """
version: 1
defaults:
  timezone: America/Bogota
  chat_id: "${TELEGRAM_CHAT_ID}"
reminders:
  - id: lunes-a
    name: "Lunes A"
    cron: "0 7 * * 1"
    every_weeks: 2
    week_offset: 0
    anchor: 2026-08-03
    message: "basura"
  - id: lunes-b
    cron: "0 7 * * 1"
    every_weeks: 2
    week_offset: 1
    anchor: 2026-08-03
    message: "reciclaje"
    chat_id: "-100999"
"""


def write(tmp_path: Path, contenido: str) -> Path:
    ruta = tmp_path / "reminders.yaml"
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def test_carga_un_archivo_valido(tmp_path):
    recordatorios = load_reminders(write(tmp_path, VALIDO), env={"TELEGRAM_CHAT_ID": "555"})

    assert [r.id for r in recordatorios] == ["lunes-a", "lunes-b"]
    a, b = recordatorios
    assert a.chat_id == "555"  # viene de defaults, con ${VAR} resuelto
    assert b.chat_id == "-100999"  # el propio pisa al default
    assert a.name == "Lunes A"
    assert b.name == "lunes-b"  # sin name, cae al id
    assert a.anchor == date(2026, 8, 3)
    assert a.timezone == "America/Bogota"
    assert a.enabled is True


def test_variable_de_entorno_faltante_es_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, VALIDO), env={})
    assert any("TELEGRAM_CHAT_ID" in p for p in exc.value.problems)


def test_reporta_todos_los_problemas_de_una_vez(tmp_path):
    roto = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: sin-cron
    message: "hola"
  - id: cron-malo
    cron: "esto no es cron"
    message: "hola"
  - id: zona-rara
    cron: "0 7 * * 1"
    timezone: America/Narnia
    message: "hola"
  - id: sin-mensaje
    cron: "0 7 * * 1"
  - id: sin-cron
    cron: "0 7 * * 1"
    message: "duplicado"
"""
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, roto), env={})

    problemas = "\n".join(exc.value.problems)
    assert "'cron' es obligatorio" in problemas
    assert "cron inválido" in problemas
    assert "zona horaria desconocida" in problemas
    assert "'message' es obligatorio" in problemas
    assert "está repetido" in problemas


def test_week_offset_fuera_de_rango(tmp_path):
    contenido = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: imposible
    cron: "0 7 * * 1"
    every_weeks: 2
    week_offset: 2
    message: "nunca"
"""
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert any("week_offset debe estar entre 0 y 1" in p for p in exc.value.problems)


def test_clave_desconocida_se_detecta(tmp_path):
    contenido = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: typo
    cron: "0 7 * * 1"
    message: "hola"
    every_week: 2
"""
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert any("clave desconocida 'every_week'" in p for p in exc.value.problems)


def test_cron_de_seis_campos_se_rechaza(tmp_path):
    contenido = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: con-segundos
    cron: "0 0 7 * * 1"
    message: "hola"
"""
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert any("5 campos" in p for p in exc.value.problems)


def test_archivo_inexistente(tmp_path):
    with pytest.raises(ConfigError):
        load_reminders(tmp_path / "no-existe.yaml", env={})


def test_el_reminders_yaml_del_repo_es_valido():
    raiz = Path(__file__).resolve().parents[1]
    recordatorios = load_reminders(
        raiz / "reminders.yaml",
        env={
            "TELEGRAM_CHAT_ID": "0",
            "PERSONA_1": "Persona-1",
            "PERSONA_2": "Persona-2",
            "PERSONA_3": "Persona-3",
            "PERSONA_4": "Persona-4",
        },
    )

    por_id = {r.id: r for r in recordatorios}
    manana, tarde = por_id["bano-manana"], por_id["bano-tarde"]

    # Los dos avisos del baño tienen que compartir turno: misma gente, mismo
    # anchor y los mismos días. Si divergen, la mañana y la tarde nombrarían a
    # personas distintas el mismo día.
    assert manana.rotation == tarde.rotation == ("Persona-1", "Persona-2", "Persona-3")
    assert manana.anchor == tarde.anchor == date(2026, 8, 3)
    assert manana.cron.split()[-1] == tarde.cron.split()[-1] == "1,4"


def test_falta_un_nombre_de_la_rotacion(tmp_path):
    contenido = """
version: 1
defaults:
  chat_id: "1"
reminders:
  - id: bano
    cron: "0 6 * * 1,4"
    rotation: ["${PERSONA_1}", "${PERSONA_2}"]
    anchor: 2026-08-03
    message: "le toca a {turno}"
"""
    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={"PERSONA_1": "Ana"})
    assert any("PERSONA_2" in p for p in exc.value.problems)


def _con_poll(cuerpo: str) -> str:
    return f"""
version: 1
defaults:
  chat_id: "555"
reminders:
  - id: basura
    cron: "0 7 * * 1"
{cuerpo}
"""


def test_poll_valido_queda_en_el_recordatorio(tmp_path):
    contenido = _con_poll(
        '    message: "¿sacas la basura?"\n'
        "    poll:\n"
        "      options:\n"
        '        - "Sí"\n'
        '        - "No"\n'
    )

    reminder = load_reminders(write(tmp_path, contenido), env={})[0]

    assert reminder.is_poll is True
    assert reminder.poll_options == ("Sí", "No")


def test_una_encuesta_de_una_sola_respuesta_es_error(tmp_path):
    contenido = _con_poll(
        '    message: "¿sacas la basura?"\n'
        "    poll:\n"
        "      options:\n"
        '        - "Sí"\n'
    )

    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert "entre 2 y 12 respuestas" in exc.value.report()


def test_una_respuesta_demasiado_larga_es_error(tmp_path):
    contenido = _con_poll(
        '    message: "¿sacas la basura?"\n'
        "    poll:\n"
        "      options:\n"
        f'        - "{"x" * 101}"\n'
        '        - "No"\n'
    )

    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert "admite 100" in exc.value.report()


def test_una_pregunta_larga_se_mide_con_el_nombre_puesto(tmp_path):
    # El mensaje crudo entra en 300, pero con el nombre sustituido no. Medirlo
    # sin sustituir dejaría pasar un YAML que después falla al enviar.
    plantilla = "{turno} " + "x" * 285
    assert len(plantilla) <= 300
    contenido = _con_poll(
        f'    message: "{plantilla}"\n'
        "    rotation: [Ana, Maria Fernanda Restrepo]\n"
        "    anchor: 2026-08-03\n"
        "    poll:\n"
        "      options:\n"
        '        - "Sí"\n'
        '        - "No"\n'
    )

    with pytest.raises(ConfigError) as exc:
        load_reminders(write(tmp_path, contenido), env={})
    assert "admite 300" in exc.value.report()


def test_las_etiquetas_html_no_cuentan_para_la_pregunta(tmp_path):
    # Una encuesta no interpreta HTML, así que el <b> ni llega: no debería
    # gastar caracteres del límite.
    contenido = _con_poll(
        f'    message: "<b>{"x" * 295}</b>"\n'
        "    poll:\n"
        "      options:\n"
        '        - "Sí"\n'
        '        - "No"\n'
    )

    reminder = load_reminders(write(tmp_path, contenido), env={})[0]

    assert len(reminder.render_question()) == 295


def test_las_encuestas_del_repo_solo_ofrecen_novedades():
    """Ninguna opción confirma que sí se va a hacer la tarea.

    Es una decisión, no un descuido: el silencio significa "lo hago". Una
    encuesta que hay que contestar todos los días se termina dejando de
    contestar, y entonces tampoco se ve la respuesta que sí importaba. Este test
    existe para que volver a agregar un "sí" sea una decisión explícita y no un
    descuido al editar el YAML.
    """
    raiz = Path(__file__).resolve().parents[1]
    recordatorios = load_reminders(
        raiz / "reminders.yaml",
        env={
            "TELEGRAM_CHAT_ID": "0",
            "PERSONA_1": "Persona-1",
            "PERSONA_2": "Persona-2",
            "PERSONA_3": "Persona-3",
            "PERSONA_4": "Persona-4",
        },
    )

    con_encuesta = [r for r in recordatorios if r.is_poll]
    assert con_encuesta, "se esperaban recordatorios con encuesta"

    for reminder in con_encuesta:
        for opcion in reminder.poll_options:
            assert not opcion.startswith("✅"), f"{reminder.id}: {opcion!r} parece una confirmación"
            assert "Sí," not in opcion, f"{reminder.id}: {opcion!r} parece una confirmación"
