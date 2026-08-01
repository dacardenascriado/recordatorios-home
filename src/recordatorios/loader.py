"""Lectura y validación de reminders.yaml."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

from recordatorios.config import ConfigError
from recordatorios.models import (
    DEFAULT_ANCHOR,
    DEFAULT_MAX_DELAY_MINUTES,
    DEFAULT_TIMEZONE,
    TURNO,
    Reminder,
)

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PARSE_MODES = {"HTML", "Markdown", "MarkdownV2"}

REMINDER_KEYS = {
    "id",
    "name",
    "enabled",
    "cron",
    "timezone",
    "every_weeks",
    "week_offset",
    "anchor",
    "rotation",
    "starts_on",
    "ends_on",
    "message",
    "chat_id",
    "max_delay_minutes",
    "parse_mode",
    "silent",
}
DEFAULTS_KEYS = REMINDER_KEYS - {
    "id",
    "name",
    "cron",
    "every_weeks",
    "week_offset",
    "message",
    "rotation",
}
TOP_LEVEL_KEYS = {"version", "defaults", "reminders"}


def load_reminders(path: str | Path, env: dict[str, str] | None = None) -> list[Reminder]:
    """Carga los recordatorios del YAML. Reporta todos los errores de una vez."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"No encuentro el archivo de recordatorios: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} no es YAML válido: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} debe empezar con un mapa (version/defaults/reminders)")

    problems: list[str] = []
    _check_unknown(raw, TOP_LEVEL_KEYS, "raíz", problems)

    version = raw.get("version", 1)
    if version != 1:
        problems.append(f"version {version!r} no soportada (esperaba 1)")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.append("defaults debe ser un mapa")
        defaults = {}
    else:
        _check_unknown(defaults, DEFAULTS_KEYS, "defaults", problems)

    entries = raw.get("reminders")
    if entries is None:
        problems.append("falta la lista 'reminders'")
        entries = []
    elif not isinstance(entries, list):
        problems.append("'reminders' debe ser una lista")
        entries = []

    env = dict(os.environ if env is None else env)
    reminders: list[Reminder] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"reminders[{index}]: cada recordatorio debe ser un mapa")
            continue

        merged = {**defaults, **entry}
        rid = merged.get("id")
        where = f"reminders[{index}]" + (f" (id={rid})" if isinstance(rid, str) else "")

        _check_unknown(entry, REMINDER_KEYS, where, problems)

        if not isinstance(rid, str) or not ID_PATTERN.match(rid):
            problems.append(
                f"{where}: 'id' es obligatorio, en minúsculas, 2-64 caracteres [a-z0-9_-]"
            )
            continue
        if rid in seen:
            problems.append(f"{where}: el id '{rid}' está repetido")
            continue
        seen.add(rid)

        reminder = _build(rid, where, merged, env, problems)
        if reminder is not None:
            reminders.append(reminder)

    if problems:
        raise ConfigError(f"{path} tiene {len(problems)} problema(s):", problems)

    return reminders


def _build(
    rid: str,
    where: str,
    data: dict[str, Any],
    env: dict[str, str],
    problems: list[str],
) -> Reminder | None:
    before = len(problems)

    cron = data.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        problems.append(f"{where}: 'cron' es obligatorio (ej. '0 7 * * 1')")
        cron = ""
    elif not croniter.is_valid(cron):
        problems.append(f"{where}: cron inválido: {cron!r}")
    elif len(cron.split()) != 5:
        problems.append(f"{where}: usa cron de 5 campos (min hora día mes día-semana), no {cron!r}")

    tz_name = data.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(tz_name, str):
        problems.append(f"{where}: 'timezone' debe ser texto")
        tz_name = DEFAULT_TIMEZONE
    else:
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            problems.append(
                f"{where}: zona horaria desconocida {tz_name!r}. Usá un nombre IANA "
                f"(America/Bogota); si estás en Windows y el nombre es correcto, "
                f"falta instalar el paquete 'tzdata'"
            )
        except ValueError:
            problems.append(f"{where}: {tz_name!r} no es un nombre de zona horaria válido")

    messages = _messages(data.get("message"), where, env, problems)
    rotation = _rotation(data.get("rotation"), where, env, problems)

    # Con turnos o con varios mensajes hay que saber desde cuándo se cuenta, y
    # el anchor por defecto (1970) obligaría a recorrer medio siglo de fechas.
    if (rotation or len(messages) > 1) and data.get("anchor") is None:
        problems.append(
            f"{where}: al usar 'rotation' o varios mensajes hace falta 'anchor' "
            f"(la fecha del primer turno, AAAA-MM-DD)"
        )
    if rotation and not any(TURNO in texto for texto in messages):
        problems.append(
            f"{where}: declaraste 'rotation' pero ningún mensaje usa {TURNO}, "
            f"así que a nadie le quedaría claro a quién le toca"
        )

    marca = len(problems)
    chat_id = _expand(data.get("chat_id"), where, "chat_id", env, problems)
    if not chat_id and len(problems) == marca:
        problems.append(
            f"{where}: falta 'chat_id' (ponlo en defaults como '${{TELEGRAM_CHAT_ID}}')"
        )

    every_weeks = _positive_int(data.get("every_weeks", 1), where, "every_weeks", problems)
    week_offset = _non_negative_int(data.get("week_offset", 0), where, "week_offset", problems)
    if every_weeks and week_offset is not None and week_offset >= every_weeks:
        problems.append(
            f"{where}: week_offset debe estar entre 0 y {every_weeks - 1} "
            f"cuando every_weeks={every_weeks}"
        )

    anchor = _date(data.get("anchor", DEFAULT_ANCHOR), where, "anchor", problems) or DEFAULT_ANCHOR
    starts_on = _date(data.get("starts_on"), where, "starts_on", problems)
    ends_on = _date(data.get("ends_on"), where, "ends_on", problems)
    if starts_on and ends_on and starts_on > ends_on:
        problems.append(f"{where}: starts_on ({starts_on}) es posterior a ends_on ({ends_on})")

    max_delay = _positive_int(
        data.get("max_delay_minutes", DEFAULT_MAX_DELAY_MINUTES),
        where,
        "max_delay_minutes",
        problems,
    )

    parse_mode = data.get("parse_mode", "HTML")
    if parse_mode in (None, False, "", "none"):
        parse_mode = None
    elif parse_mode not in PARSE_MODES:
        problems.append(
            f"{where}: parse_mode {parse_mode!r} inválido "
            f"(usa {', '.join(sorted(PARSE_MODES))} o none)"
        )
        parse_mode = None

    enabled = _bool(data.get("enabled", True), where, "enabled", problems)
    silent = _bool(data.get("silent", False), where, "silent", problems)

    if len(problems) > before:
        return None

    return Reminder(
        id=rid,
        name=str(data.get("name") or rid),
        cron=cron,
        messages=messages,
        chat_id=chat_id or "",
        timezone=tz_name,
        enabled=bool(enabled),
        every_weeks=every_weeks or 1,
        week_offset=week_offset or 0,
        anchor=anchor,
        rotation=rotation,
        starts_on=starts_on,
        ends_on=ends_on,
        max_delay_minutes=max_delay or DEFAULT_MAX_DELAY_MINUTES,
        parse_mode=parse_mode,
        silent=bool(silent),
    )


def _messages(
    value: Any, where: str, env: dict[str, str], problems: list[str]
) -> tuple[str, ...]:
    """'message' acepta un texto o una lista de textos que se van turnando."""
    if value is None:
        problems.append(f"{where}: 'message' es obligatorio")
        return ()

    crudos = value if isinstance(value, list) else [value]
    if not crudos:
        problems.append(f"{where}: la lista de 'message' está vacía")
        return ()

    textos: list[str] = []
    for i, crudo in enumerate(crudos):
        etiqueta = "message" if len(crudos) == 1 else f"message[{i}]"
        texto = _expand(crudo, where, etiqueta, env, problems)
        if not texto:
            problems.append(f"{where}: {etiqueta} está vacío")
        else:
            textos.append(texto)
    return tuple(textos)


def _rotation(
    value: Any, where: str, env: dict[str, str], problems: list[str]
) -> tuple[str, ...]:
    """Los nombres admiten ${VARIABLE}, para no publicarlos en un repo público."""
    if value is None:
        return ()
    if not isinstance(value, list):
        problems.append(f"{where}: 'rotation' debe ser una lista de nombres")
        return ()
    if len(value) < 2:
        problems.append(f"{where}: 'rotation' necesita al menos 2 nombres")
        return ()

    nombres: list[str] = []
    for i, crudo in enumerate(value):
        marca = len(problems)
        nombre = _expand(crudo, where, f"rotation[{i}]", env, problems)
        if not nombre and len(problems) == marca:
            problems.append(f"{where}: {crudo!r} no es un nombre válido en 'rotation'")
        elif nombre:
            nombres.append(nombre)
    return tuple(nombres)


def _check_unknown(data: dict[str, Any], allowed: set[str], where: str, problems: list[str]) -> None:
    for key in data:
        if key not in allowed:
            problems.append(f"{where}: clave desconocida {key!r} (¿un typo?)")


def _expand(
    value: Any, where: str, field: str, env: dict[str, str], problems: list[str]
) -> str | None:
    """Convierte a texto y reemplaza ${VAR} por variables de entorno."""
    if value is None:
        return None
    text = str(value)
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = env.get(name)
        if resolved is None or not resolved.strip():
            missing.append(name)
            return ""
        return resolved.strip()

    text = ENV_PATTERN.sub(replace, text)
    for name in missing:
        problems.append(f"{where}: {field} usa ${{{name}}} pero esa variable no está definida")
    return text.strip() or None


def _positive_int(value: Any, where: str, field: str, problems: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        problems.append(f"{where}: {field} debe ser un entero >= 1, no {value!r}")
        return None
    return value


def _non_negative_int(value: Any, where: str, field: str, problems: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.append(f"{where}: {field} debe ser un entero >= 0, no {value!r}")
        return None
    return value


def _bool(value: Any, where: str, field: str, problems: list[str]) -> bool:
    if not isinstance(value, bool):
        problems.append(f"{where}: {field} debe ser true o false, no {value!r}")
        return True
    return value


def _date(value: Any, where: str, field: str, problems: list[str]) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    problems.append(f"{where}: {field} debe ser una fecha AAAA-MM-DD, no {value!r}")
    return None
