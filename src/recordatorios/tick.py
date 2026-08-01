"""El tick: qué recordatorios vencieron hace poco y todavía no se enviaron.

GitHub Actions no es puntual — un cron de */5 puede correr con 10 o 20 minutos
de retraso, o saltarse una corrida. Por eso el tick no pregunta "¿toca justo
ahora?" sino "¿qué ocurrencias cayeron en las últimas N horas?". Un retraso se
recupera en la corrida siguiente en vez de perderse.

De esas ocurrencias, la base de datos dice cuáles ya se enviaron. La clave está
en el orden: primero se calcula todo con el YAML en la mano, y solo si quedó
algo pendiente se abre la conexión. La inmensa mayoría de los ticks no tiene
nada que hacer y termina sin tocar la base — que es lo que mantiene el consumo
de Neon en casi cero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from recordatorios.config import Settings
from recordatorios.models import Reminder
from recordatorios.schedule import local_str, occurrences_between, turn_index
from recordatorios.store import Store
from recordatorios.telegram import Sender


@dataclass
class Outcome:
    reminder: Reminder
    occurrence_at: datetime
    status: str
    detail: str | None = None

    def line(self) -> str:
        cuando = local_str(self.occurrence_at, self.reminder.timezone)
        base = f"[{self.status}] {self.reminder.label} — programado {cuando}"
        return f"{base} — {self.detail}" if self.detail else base


@dataclass
class TickResult:
    window_start: datetime
    window_end: datetime
    outcomes: list[Outcome] = field(default_factory=list)
    dry_run: bool = False
    touched_database: bool = False

    def of(self, status: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def failures(self) -> list[Outcome]:
        return self.of("failed")

    def report(self) -> str:
        modo = " (simulación)" if self.dry_run else ""
        lineas = [f"Ventana{modo}: {_iso(self.window_start)} → {_iso(self.window_end)}"]

        if not self.outcomes:
            lineas.append("Nada pendiente. No se abrió conexión a la base.")
            return "\n".join(lineas)

        lineas.extend(f"  {o.line()}" for o in self.outcomes)
        conteo: dict[str, int] = {}
        for o in self.outcomes:
            conteo[o.status] = conteo.get(o.status, 0) + 1
        lineas.append("Resumen: " + ", ".join(f"{v} {k}" for k, v in sorted(conteo.items())))
        return "\n".join(lineas)


def run_tick(
    reminders: list[Reminder],
    store: Store,
    sender: Sender,
    settings: Settings,
    now: datetime | None = None,
    dry_run: bool = False,
) -> TickResult:
    """Procesa la ventana de recuperación. `store` se conecta de forma perezosa:
    si no hay nada que enviar, esta función no lo usa y nunca llega a abrirse."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(minutes=_lookback_minutes(settings))
    result = TickResult(window_start=start, window_end=now, dry_run=dry_run)

    candidatas: list[tuple[Reminder, datetime]] = []
    for reminder in reminders:
        if not reminder.enabled:
            continue
        for occurrence in occurrences_between(reminder, start, now):
            candidatas.append((reminder, occurrence))
    candidatas.sort(key=lambda item: (item[1], item[0].id))

    # Lo que llegó demasiado tarde se descarta acá, sin consultar nada: si no lo
    # vamos a enviar, da igual si ya se había enviado.
    pendientes: list[tuple[Reminder, datetime]] = []
    for reminder, occurrence in candidatas:
        retraso = now - occurrence
        if retraso > timedelta(minutes=reminder.max_delay_minutes):
            detalle = (
                f"retraso {_minutos(retraso)} > max_delay_minutes "
                f"({reminder.max_delay_minutes} min)"
            )
            result.outcomes.append(Outcome(reminder, occurrence, "skipped_stale", detalle))
        else:
            pendientes.append((reminder, occurrence))

    if not pendientes:
        return result

    if dry_run:
        result.outcomes.extend(
            Outcome(r, occ, "would_send", f"retraso {_minutos(now - occ)}")
            for r, occ in pendientes
        )
        return result

    result.touched_database = True
    store.init_schema()
    store.prune(now)
    for reminder, occurrence in pendientes:
        result.outcomes.append(_deliver(reminder, occurrence, store, sender, now))

    return result


def _deliver(
    reminder: Reminder,
    occurrence: datetime,
    store: Store,
    sender: Sender,
    now: datetime,
) -> Outcome:
    # El claim es la garantía de no duplicar: si otra corrida ya se encargó de
    # esta ocurrencia exacta, acá se corta.
    if not store.claim(reminder.id, occurrence, now):
        return Outcome(reminder, occurrence, "already_handled")

    turno = turn_index(reminder, occurrence) if reminder.needs_turn else 0

    try:
        sender.send_message(
            chat_id=reminder.chat_id,
            text=reminder.render(turno),
            parse_mode=reminder.parse_mode,
            silent=reminder.silent,
        )
    except Exception as exc:  # el envío no debe tumbar el resto del tick
        detalle = f"{type(exc).__name__}: {exc}"
        # Queda en 'failed': sigue dentro de la ventana, así que el próximo tick
        # lo reintenta hasta que se envíe o se pase de max_delay_minutes.
        store.mark_failed(reminder.id, occurrence, detalle, now)
        return Outcome(reminder, occurrence, "failed", detalle)

    store.mark_sent(reminder.id, occurrence, now)
    quien = reminder.whose_turn(turno)
    detalle = f"retraso {_minutos(now - occurrence)}"
    return Outcome(reminder, occurrence, "sent", f"turno de {quien}, {detalle}" if quien else detalle)


def _lookback_minutes(settings: Settings) -> int:
    """Cuánto hacia atrás mira cada tick.

    Es la tolerancia a que Actions se atrase o se caiga: un hueco más largo que
    esto pierde los recordatorios que hayan caído dentro. Agrandarlo también
    alarga el rato que la base queda despierta después de cada envío, así que el
    tope duro de `max_window_hours` existe para que no se dispare por accidente.
    """
    return min(settings.lookback_minutes, settings.max_window_hours * 60)


def _minutos(delta: timedelta) -> str:
    return f"{int(delta.total_seconds() // 60)} min"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
