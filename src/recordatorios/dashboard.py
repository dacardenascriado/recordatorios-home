"""El dashboard: cruzar lo que debía salir contra lo que salió.

Existe por lo que pasó en agosto de 2026. Actions dejó de correr el tick, todas
las corridas figuraban en verde, y la única forma de enterarse fue que alguien
notó que no había llegado un mensaje. Averiguar qué había pasado costó consultar
la API de GitHub a mano.

La pregunta que contesta esta página es justamente esa: de todo lo que el
calendario decía que tenía que salir, ¿qué salió? Un recordatorio esperado sin
fila en la base es el peor caso —se perdió sin dejar rastro— y acá aparece como
`perdido`, que es la única forma de que se vea.

NO LLEVA NOMBRES. El repo es público y Pages también, así que los nombres de
`${PERSONA_n}` —que viven en secrets justamente para no estar acá— no entran.
La página habla de ids de recordatorio y de horas; a quién le toca ya lo dice el
mensaje de Telegram.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from recordatorios.models import Reminder
from recordatorios.schedule import next_runs, occurrences_between
from recordatorios.store import DeliveryRow, Store

# Estados que puede tener una ocurrencia esperada, del mejor al peor.
ENVIADO = "enviado"
PERDIDO = "perdido"
VENCIDO = "vencido"
FALLIDO = "fallido"
EN_CURSO = "en curso"

# Una ocurrencia recién vencida todavía puede estar esperando su tick: marcarla
# como perdida enseguida sería una falsa alarma. Se le da este margen antes de
# contarla como problema.
GRACIA_MINUTOS = 20


@dataclass(frozen=True)
class Fila:
    """Una ocurrencia esperada y en qué terminó."""

    reminder_id: str
    occurrence_at: datetime
    estado: str
    detalle: str | None
    entregado_at: datetime | None

    @property
    def es_problema(self) -> bool:
        return self.estado in (PERDIDO, VENCIDO, FALLIDO)


@dataclass(frozen=True)
class Resumen:
    generado_at: datetime
    desde: datetime
    hasta: datetime
    pasado: list[Fila]
    futuro: list[tuple[str, datetime]]
    ultima_entrega: datetime | None

    @property
    def problemas(self) -> list[Fila]:
        return [f for f in self.pasado if f.es_problema]

    @property
    def salud(self) -> str:
        """Una palabra para el encabezado: es lo único que mucha gente va a leer."""
        if not self.pasado:
            return "sin datos"
        return "con problemas" if self.problemas else "al día"


def construir(
    reminders: list[Reminder],
    store: Store,
    now: datetime | None = None,
    dias_atras: int = 7,
    dias_adelante: int = 14,
) -> Resumen:
    """Cruza el calendario contra la base. No envía ni escribe nada."""
    now = now or datetime.now(timezone.utc)
    desde = now - timedelta(days=dias_atras)

    esperadas: list[tuple[Reminder, datetime]] = []
    for reminder in reminders:
        if not reminder.enabled:
            continue
        for occurrence in occurrences_between(reminder, desde, now):
            esperadas.append((reminder, occurrence))
    esperadas.sort(key=lambda item: (item[1], item[0].id))

    store.init_schema()
    registradas = {
        (row.reminder_id, _utc(row.occurrence_at)): row
        for row in store.deliveries_between(desde, now)
    }

    pasado = [
        _clasificar(reminder, occurrence, registradas.get((reminder.id, _utc(occurrence))), now)
        for reminder, occurrence in esperadas
    ]
    pasado.reverse()  # lo más reciente primero: es lo que uno viene a mirar

    futuro: list[tuple[str, datetime]] = []
    for reminder in reminders:
        if not reminder.enabled:
            continue
        limite = now + timedelta(days=dias_adelante)
        for occurrence in next_runs(reminder, count=60, after=now):
            if occurrence > limite:
                break
            futuro.append((reminder.id, occurrence))
    futuro.sort(key=lambda item: (item[1], item[0]))

    entregas = [f.entregado_at for f in pasado if f.entregado_at]
    return Resumen(
        generado_at=now,
        desde=desde,
        hasta=now,
        pasado=pasado,
        futuro=futuro,
        ultima_entrega=max(entregas) if entregas else None,
    )


def _clasificar(
    reminder: Reminder, occurrence: datetime, row: DeliveryRow | None, now: datetime
) -> Fila:
    if row is None:
        # Sin fila en la base: o el tick todavía no llegó, o nunca llegó. La
        # diferencia es cuánto hace que venció.
        vencida = now - occurrence > timedelta(minutes=reminder.max_delay_minutes + GRACIA_MINUTOS)
        return Fila(
            reminder_id=reminder.id,
            occurrence_at=occurrence,
            estado=PERDIDO if vencida else EN_CURSO,
            detalle="ningún tick la vio" if vencida else None,
            entregado_at=None,
        )

    estado = {
        "sent": ENVIADO,
        "stale": VENCIDO,
        "failed": FALLIDO,
        "sending": EN_CURSO,
    }.get(row.status, row.status)
    return Fila(
        reminder_id=reminder.id,
        occurrence_at=occurrence,
        estado=estado,
        detalle=row.detail,
        entregado_at=row.logged_at if estado == ENVIADO else None,
    )


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)
