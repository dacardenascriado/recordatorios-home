"""Descartar: que no diga que hizo algo cuando no hizo nada.

El comando existe para sacar de la lista una pérdida que ya no se puede
recuperar. Su peor forma de fallar no es reventar, es salir en verde sin haber
cambiado nada — que es como se descubrió que el dashboard no se refrescaba.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recordatorios import cli
from recordatorios.store import Store

UTC = timezone.utc

# El aviso de la tarde del baño: lunes y jueves a las 17:00 en Bogotá.
YAML = """
version: 1
defaults:
  chat_id: "555"
reminders:
  - id: bano-tarde
    cron: "0 17 * * 1,4"
    message: "¿el baño ya quedó?"
"""

# Jueves 2026-08-27 17:00 en Bogotá.
OCURRENCIA = datetime(2026, 8, 27, 22, 0, tzinfo=UTC)
REF = f"bano-tarde@{OCURRENCIA.isoformat()}"


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    (tmp_path / "reminders.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REMINDERS_FILE", str(tmp_path / "reminders.yaml"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp_path


def estado(tmp_path, reminder_id, occurrence):
    """Estado guardado de una ocurrencia, o None si no hay fila.

    Crea el esquema primero: cuando el comando se planta antes de tocar la base
    —que es justo lo que varios de estos tests comprueban— las tablas ni
    existen, y eso es un resultado válido, no un error.
    """
    with Store.open(None, sqlite_path=tmp_path / "recordatorios.db") as store:
        store.init_schema()
        filas = store.deliveries_between(
            occurrence - timedelta(minutes=1), occurrence + timedelta(minutes=1)
        )
    return next((f.status for f in filas if f.reminder_id == reminder_id), None)


def test_descarta_una_ocurrencia_real(entorno):
    assert cli.main(["descartar", "--ref", REF]) == 0
    assert estado(entorno, "bano-tarde", OCURRENCIA) == "dismissed"


def test_un_id_inexistente_no_pasa_en_silencio(entorno, capsys):
    codigo = cli.main(["descartar", "--ref", f"no-existe@{OCURRENCIA.isoformat()}"])

    assert codigo == 1
    assert "No existe el recordatorio" in capsys.readouterr().err


def test_una_hora_en_la_que_no_dispara_no_pasa_en_silencio(entorno, capsys):
    # Una referencia mal pegada insertaría una fila que no le corresponde a
    # ninguna ocurrencia: el comando saldría en verde y el dashboard no
    # cambiaría. Peor que fallar.
    codigo = cli.main(["descartar", "--ref", "bano-tarde@2026-08-27T21:37:00+00:00"])

    assert codigo == 1
    assert "no dispara en" in capsys.readouterr().err
    assert estado(entorno, "bano-tarde", datetime(2026, 8, 27, 21, 37, tzinfo=UTC)) is None


def test_descartar_algo_que_si_salio_termina_en_rojo(entorno, capsys):
    # Es un malentendido, no una rutina: conviene que la corrida quede en rojo.
    with Store.open(None, sqlite_path=entorno / "recordatorios.db") as store:
        store.init_schema()
        store.claim("bano-tarde", OCURRENCIA, OCURRENCIA)
        store.mark_sent("bano-tarde", OCURRENCIA, OCURRENCIA)

    codigo = cli.main(["descartar", "--ref", REF])

    assert codigo == 1
    assert "ese sí salió" in capsys.readouterr().out
    assert estado(entorno, "bano-tarde", OCURRENCIA) == "sent"


def test_descartar_dos_veces_no_es_un_fallo(entorno):
    assert cli.main(["descartar", "--ref", REF]) == 0
    assert cli.main(["descartar", "--ref", REF]) == 0


def test_hace_falta_ref_o_before_pero_no_los_dos(entorno, capsys):
    assert cli.main(["descartar"]) == 1
    assert cli.main(["descartar", "--ref", REF, "--before", "2026-08-28"]) == 1
    assert "exactamente uno" in capsys.readouterr().err


def test_before_descarta_todo_lo_anterior_a_la_fecha(entorno):
    # El jueves 27 tiene ocurrencia a las 17:00 Bogotá; el lunes 24 también.
    lunes = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)

    assert cli.main(["descartar", "--before", "2026-08-28"]) == 0

    assert estado(entorno, "bano-tarde", OCURRENCIA) == "dismissed"
    assert estado(entorno, "bano-tarde", lunes) == "dismissed"


def test_before_no_toca_lo_del_dia_del_corte_ni_lo_posterior(entorno):
    # El corte es estricto: lo del propio día todavía puede servir.
    assert cli.main(["descartar", "--before", "2026-08-27"]) == 0

    assert estado(entorno, "bano-tarde", OCURRENCIA) is None


def test_before_sin_nada_que_descartar_no_es_un_fallo(entorno, capsys):
    assert cli.main(["descartar", "--before", "2020-01-01"]) == 0
    assert "No hay nada sin salir" in capsys.readouterr().out


def test_before_con_una_fecha_ilegible_se_planta(entorno, capsys):
    assert cli.main(["descartar", "--before", "28/08/2026"]) == 1
    assert "espera AAAA-MM-DD" in capsys.readouterr().err
