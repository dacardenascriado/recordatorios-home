"""Configuración leída del entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REMINDERS_FILE = "reminders.yaml"

# Ventana de recuperación: cuánto hacia atrás mira cada tick. Define la
# tolerancia a que GitHub Actions se atrase o se caiga; un hueco más largo que
# esto pierde los recordatorios que hayan caído dentro.
#
# Tiene que ser MAYOR que el max_delay_minutes de los recordatorios. Si son
# iguales, toda ocurrencia dentro de la ventana está por definición dentro del
# plazo, y la que se pasó del plazo ya cayó fuera de la ventana: el tick no la
# ve nunca y se pierde en silencio, sin fila en la base ni línea en el log. Con
# la ventana más ancha queda un margen en el que la ocurrencia todavía se ve,
# se descarta a conciencia y se registra que se perdió.
DEFAULT_LOOKBACK_MINUTES = 240

# Tope duro de la ventana. Evita que un valor mal puesto reviva recordatorios de
# hace días y mantenga la base despierta de más.
MAX_WINDOW_HOURS = 6


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None
    database_url: str | None
    reminders_file: Path
    lookback_minutes: int
    max_window_hours: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        return cls(
            telegram_token=_clean(env.get("TELEGRAM_BOT_TOKEN")),
            database_url=_clean(env.get("DATABASE_URL")),
            reminders_file=Path(env.get("REMINDERS_FILE") or DEFAULT_REMINDERS_FILE),
            lookback_minutes=_int(env, "TICK_LOOKBACK_MINUTES", DEFAULT_LOOKBACK_MINUTES),
            max_window_hours=_int(env, "TICK_MAX_WINDOW_HOURS", MAX_WINDOW_HOURS),
        )

    def require_token(self) -> str:
        if not self.telegram_token:
            raise ConfigError(
                "Falta TELEGRAM_BOT_TOKEN. En local ponlo en .env; "
                "en GitHub, en Settings > Secrets and variables > Actions."
            )
        return self.telegram_token


class ConfigError(Exception):
    """Error de configuración con mensaje presentable al usuario."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []

    def report(self) -> str:
        if not self.problems:
            return str(self)
        detalle = "\n".join(f"  - {p}" for p in self.problems)
        return f"{self}\n{detalle}"


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = _clean(env.get(key))
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} debe ser un entero, no {raw!r}") from exc


def load_dotenv(path: str | Path = ".env") -> None:
    """Carga un .env sencillo para uso local. No pisa variables ya definidas."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
