"""El comando `check` es el que le dice al usuario "está todo bien".

Justamente por eso conviene que no mienta: acá se comprueba que devuelva 0 solo
cuando de verdad respondieron Telegram y la base, y 1 en cuanto algo falla.
"""

from __future__ import annotations

import pytest

from recordatorios import cli
from recordatorios.telegram import TelegramError

YAML = """
version: 1
defaults:
  chat_id: "${TELEGRAM_CHAT_ID}"
reminders:
  - id: basura
    cron: "0 7 * * 1"
    message: "sacar la basura"
"""


class FakeSender:
    """Reemplaza a TelegramSender. Puede fallar en getMe o en getChat."""

    falla_get_me = False
    falla_get_chat = False

    def __init__(self, token: str) -> None:
        self.token = token

    def get_me(self):
        if type(self).falla_get_me:
            raise TelegramError("token inválido")
        return {"username": "recordatorios_home_bot"}

    def get_chat(self, chat_id: str):
        if type(self).falla_get_chat:
            raise TelegramError("chat not found")
        return {"type": "supergroup", "title": "La casa"}


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    (tmp_path / "reminders.yaml").write_text(YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REMINDERS_FILE", str(tmp_path / "reminders.yaml"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100999")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    FakeSender.falla_get_me = False
    FakeSender.falla_get_chat = False
    monkeypatch.setattr(cli, "TelegramSender", FakeSender)
    return tmp_path


def test_check_pasa_cuando_todo_responde(entorno, capsys):
    assert cli.main(["check"]) == 0

    salida = capsys.readouterr().out
    assert "[OK ] reminders.yaml" in salida
    assert "@recordatorios_home_bot" in salida
    # El chat_id sale recortado: este log queda público en Actions.
    assert "[OK ] Chat …0999" in salida
    assert "-100999" not in salida
    assert "[OK ] Base de datos" in salida
    assert "Todo en orden" in salida


def test_check_falla_si_el_token_no_sirve(entorno, capsys):
    FakeSender.falla_get_me = True

    assert cli.main(["check"]) == 1
    assert "Todo en orden" not in capsys.readouterr().out


def test_check_falla_si_el_chat_no_existe(entorno, capsys):
    FakeSender.falla_get_chat = True

    assert cli.main(["check"]) == 1


def test_check_avisa_cuando_no_hay_base_de_datos_real(entorno, capsys):
    # Sin DATABASE_URL cae a SQLite local, que en Actions se pierde entre
    # corridas: pasa la revisión pero con advertencia.
    assert cli.main(["check"]) == 0
    assert "sin DATABASE_URL" in capsys.readouterr().out


def test_check_se_planta_si_el_yaml_esta_roto(entorno, capsys):
    (entorno / "reminders.yaml").write_text(
        'version: 1\nreminders:\n  - id: roto\n    cron: "no es cron"\n', encoding="utf-8"
    )

    assert cli.main(["check"]) == 1
    # Sin YAML válido no tiene sentido seguir revisando lo demás.
    assert "@recordatorios_home_bot" not in capsys.readouterr().out


YAML_MAX_DELAY_LARGO = """
version: 1
defaults:
  chat_id: "${TELEGRAM_CHAT_ID}"
reminders:
  - id: generoso
    cron: "0 7 * * 1"
    max_delay_minutes: 900
    message: "sacar la basura"
"""


def test_validate_avisa_si_el_max_delay_alcanza_la_ventana(entorno, monkeypatch, capsys):
    # Con max_delay >= ventana, lo que se vence ya salió de la ventana cuando el
    # tick lo miraría: se perdería sin fila en la base, sin log y sin aviso. Es
    # el peor modo de fallo del sistema, así que `validate` tiene que gritarlo.
    (entorno / "reminders.yaml").write_text(YAML_MAX_DELAY_LARGO, encoding="utf-8")

    assert cli.main(["validate"]) == 0

    salida = capsys.readouterr().out
    assert "generoso" in salida
    assert "no es menor que la ventana de recuperación" in salida


def test_validate_no_avisa_con_un_max_delay_sano(entorno, capsys):
    assert cli.main(["validate"]) == 0
    assert "ventana de recuperación" not in capsys.readouterr().out
