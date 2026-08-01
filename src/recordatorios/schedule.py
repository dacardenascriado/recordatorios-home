"""Cálculo de ocurrencias: cron + filtro de recurrencia semanal."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from croniter import croniter

from recordatorios.models import Reminder

# Tope de expresiones cron evaluadas por consulta. Evita que un recordatorio con
# starts_on lejano o every_weeks alto se convierta en un bucle largo.
MAX_SCAN = 5000


def monday_of(d: date) -> date:
    """Lunes de la semana de `d` (las semanas empiezan en lunes)."""
    return d - timedelta(days=d.weekday())


def week_index(reminder: Reminder, local_date: date) -> int:
    """Número de semanas completas entre el anchor y `local_date`."""
    return (monday_of(local_date) - monday_of(reminder.anchor)).days // 7


def week_matches(reminder: Reminder, local_date: date) -> bool:
    """¿La semana de `local_date` es una de las que le tocan al recordatorio?"""
    if reminder.every_weeks <= 1:
        return True
    return week_index(reminder, local_date) % reminder.every_weeks == reminder.week_offset


def iter_occurrences(reminder: Reminder, after: datetime) -> Iterator[datetime]:
    """Ocurrencias en UTC estrictamente posteriores a `after`, en orden.

    El cron se evalúa en la zona horaria del recordatorio, así que "las 7am"
    son las 7am locales aunque cambie el horario de verano.
    """
    tz = ZoneInfo(reminder.timezone)
    cursor = croniter(reminder.cron, after.astimezone(tz))

    for _ in range(MAX_SCAN):
        local = cursor.get_next(datetime)
        local_date = local.date()

        if reminder.ends_on and local_date > reminder.ends_on:
            return
        if reminder.starts_on and local_date < reminder.starts_on:
            continue
        if not week_matches(reminder, local_date):
            continue

        yield local.astimezone(timezone.utc)


def occurrences_between(reminder: Reminder, start: datetime, end: datetime) -> list[datetime]:
    """Ocurrencias en la ventana (start, end], en UTC.

    El inicio es exclusivo y el final inclusivo para que ticks consecutivos
    cubran la línea de tiempo sin huecos ni duplicados.
    """
    out: list[datetime] = []
    for occurrence in iter_occurrences(reminder, start):
        if occurrence > end:
            break
        out.append(occurrence)
    return out


def next_runs(reminder: Reminder, count: int = 3, after: datetime | None = None) -> list[datetime]:
    """Próximas `count` ejecuciones en UTC (para la agenda del CLI)."""
    after = after or datetime.now(timezone.utc)
    out: list[datetime] = []
    for occurrence in iter_occurrences(reminder, after):
        out.append(occurrence)
        if len(out) >= count:
            break
    return out


def turn_index(reminder: Reminder, occurrence: datetime) -> int:
    """Número de turno de una ocurrencia, contando días desde el anchor.

    El turno avanza una vez por **día** en que el recordatorio dispara, no una
    vez por aviso: si un mismo día suena a las 6 y a las 18, los dos avisos
    pertenecen al mismo turno y le tocan a la misma persona.
    """
    local_date = occurrence.astimezone(ZoneInfo(reminder.timezone)).date()
    if local_date <= reminder.anchor:
        return 0
    return max(len(_turn_days(reminder, local_date)) - 1, 0)


@lru_cache(maxsize=256)
def _turn_days(reminder: Reminder, until: date) -> tuple[date, ...]:
    """Días con disparo entre el anchor y `until`, inclusive y sin repetir.

    Cacheado porque los dos avisos del mismo día preguntan lo mismo.
    """
    tz = ZoneInfo(reminder.timezone)
    desde = datetime.combine(reminder.anchor, time.min, tzinfo=tz) - timedelta(microseconds=1)

    dias: list[date] = []
    for occurrence in iter_occurrences(reminder, desde.astimezone(timezone.utc)):
        dia = occurrence.astimezone(tz).date()
        if dia > until:
            break
        if not dias or dias[-1] != dia:
            dias.append(dia)
    return tuple(dias)


def describe(reminder: Reminder) -> str:
    """Resumen legible del horario, para el CLI."""
    base = f"{reminder.cron} [{reminder.timezone}]"
    if reminder.every_weeks > 1:
        base += f" · 1 de cada {reminder.every_weeks} semanas (offset {reminder.week_offset})"
    if reminder.rotation:
        base += f" · turnos: {' → '.join(reminder.rotation)} (desde {reminder.anchor})"
    if len(reminder.messages) > 1:
        base += f" · {len(reminder.messages)} mensajes rotativos"
    if reminder.starts_on:
        base += f" · desde {reminder.starts_on}"
    if reminder.ends_on:
        base += f" · hasta {reminder.ends_on}"
    return base


def local_str(moment: datetime, tz_name: str) -> str:
    """Formatea un instante UTC en la zona horaria dada."""
    return moment.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z (%a)")
