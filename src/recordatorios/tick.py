"""El tick: qué recordatorios vencieron hace poco y todavía no se enviaron.

GitHub Actions no es puntual, y a veces directamente no corre: un cron de */5
puede llegar con 20 minutos de retraso, saltarse corridas, o —como pasó en
agosto de 2026— bajar a 3 corridas en todo el día. Por eso el tick no pregunta
"¿toca justo ahora?" sino "¿qué ocurrencias cayeron en las últimas N horas?".
Un retraso se recupera en la corrida siguiente en vez de perderse.

Lo que la ventana no puede recuperar —un hueco más largo que ella— al menos se
anota y se avisa por Telegram (`_alert_losses`), para que una pérdida nunca sea
silenciosa.

De esas ocurrencias, la base de datos dice cuáles ya se enviaron. La clave está
en el orden: primero se calcula todo con el YAML en la mano, y solo si quedó
algo que enviar o que descartar se abre la conexión. La inmensa mayoría de los
ticks no tiene nada que hacer y termina sin tocar la base — que es lo que
mantiene el consumo de Neon en casi cero.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    alerts_sent: int = 0
    alert_error: str | None = None

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
        if self.alerts_sent:
            lineas.append(f"Aviso de pérdida enviado a {self.alerts_sent} chat(s).")
        if self.alert_error:
            lineas.append(f"No se pudo avisar de la pérdida: {self.alert_error}")
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
    si no hay nada que enviar ni que descartar, esta función no lo usa y nunca
    llega a abrirse."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_minutes(settings))
    result = TickResult(window_start=start, window_end=now, dry_run=dry_run)

    candidatas: list[tuple[Reminder, datetime]] = []
    for reminder in reminders:
        if not reminder.enabled:
            continue
        for occurrence in occurrences_between(reminder, start, now):
            candidatas.append((reminder, occurrence))
    candidatas.sort(key=lambda item: (item[1], item[0].id))

    # Lo que este mismo runner ya resolvió no se vuelve a preguntar a la base.
    # Sin esto, la ventana de 12 h saldría carísima: una ocurrencia ya enviada
    # sigue apareciendo durante 12 h y cada tick abriría la conexión solo para
    # confirmar lo que ya sabe, dejando el compute de Neon despierto casi 24/7
    # y agotando la cuota del plan Free a mitad de mes.
    resueltas = _load_resolved(settings.state_file) if not dry_run else {}
    ya_resueltas = [(r, occ) for r, occ in candidatas if _key(r, occ) in resueltas]
    candidatas = [(r, occ) for r, occ in candidatas if _key(r, occ) not in resueltas]
    result.outcomes.extend(
        Outcome(r, occ, "already_handled", "resuelto antes en este bloque")
        for r, occ in ya_resueltas
    )

    # Lo que llegó demasiado tarde ya no se envía, pero sí se anota: un
    # recordatorio que se pierde sin dejar rastro es el peor modo de fallo que
    # tiene este sistema, porque nadie se entera de que faltó.
    vencidas: list[tuple[Reminder, datetime, str]] = []
    pendientes: list[tuple[Reminder, datetime]] = []
    for reminder, occurrence in candidatas:
        retraso = now - occurrence
        if retraso > timedelta(minutes=reminder.max_delay_minutes):
            detalle = (
                f"retraso {_minutos(retraso)} > max_delay_minutes "
                f"({reminder.max_delay_minutes} min)"
            )
            vencidas.append((reminder, occurrence, detalle))
        else:
            pendientes.append((reminder, occurrence))

    if not vencidas and not pendientes:
        return result

    if dry_run:
        result.outcomes.extend(
            Outcome(r, occ, "skipped_stale", detalle) for r, occ, detalle in vencidas
        )
        result.outcomes.extend(
            Outcome(r, occ, "would_send", f"retraso {_minutos(now - occ)}")
            for r, occ in pendientes
        )
        return result

    result.touched_database = True
    store.init_schema()
    store.prune(now)

    # Primero las vencidas: son más viejas que las pendientes, así que el
    # informe queda en orden cronológico.
    for reminder, occurrence, detalle in vencidas:
        # Solo la primera vez deja rastro. Después sigue apareciendo en la
        # ventana durante un rato y no queremos una fila por tick.
        primera = store.mark_stale(reminder.id, occurrence, detalle, now)
        estado = "skipped_stale" if primera else "already_handled"
        result.outcomes.append(Outcome(reminder, occurrence, estado, detalle))

    for reminder, occurrence in pendientes:
        result.outcomes.append(_deliver(reminder, occurrence, store, sender, now))

    _alert_losses(result, sender)
    _save_resolved(settings.state_file, result, start)
    return result


# -- caché local de lo ya resuelto ----------------------------------------
#
# La base de datos sigue siendo la única fuente de verdad: esto es solo un
# atajo para no volver a preguntarle lo que este runner ya preguntó. Un caché
# vacío (runner nuevo, archivo borrado) no cambia el resultado, solo lo hace
# más caro. Y nunca puede provocar un envío perdido: solo se anotan estados
# terminales, así que un 'failed' jamás entra y se sigue reintentando.

TERMINALES = frozenset({"sent", "skipped_stale", "already_handled"})


def _key(reminder: Reminder, occurrence: datetime) -> str:
    return f"{reminder.id}@{occurrence.astimezone(timezone.utc).isoformat()}"


def _vigente(key: str, window_start: datetime) -> bool:
    """¿La ocurrencia de esta clave sigue dentro de la ventana?

    Una clave ilegible se tira: el caché se puede reconstruir preguntándole a
    la base, así que ante la duda conviene olvidar y no arrastrar basura.
    """
    try:
        return datetime.fromisoformat(key.rpartition("@")[2]) >= window_start
    except ValueError:
        return False


def _load_resolved(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        datos = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


def _save_resolved(path: Path | None, result: TickResult, window_start: datetime) -> None:
    """Reescribe el caché con lo resuelto, tirando lo que ya salió de la ventana."""
    if path is None:
        return
    resueltas = {
        _key(o.reminder, o.occurrence_at): o.status
        for o in result.outcomes
        if o.status in TERMINALES and o.occurrence_at >= window_start
    }
    if not resueltas:
        return
    previas = {k: v for k, v in _load_resolved(path).items() if _vigente(k, window_start)}
    previas.update(resueltas)
    try:
        Path(path).write_text(json.dumps(previas), encoding="utf-8")
    except OSError:
        pass  # el caché es un atajo, no un requisito: sin él todo sigue funcionando


def _alert_losses(result: TickResult, sender: Sender) -> None:
    """Avisa al chat cuando un recordatorio se perdió por completo.

    Sin esto, una pérdida solo existe como una línea en un log de Actions que
    nadie mira: el sistema puede estar caído días y la primera señal es que
    alguien nota que no llegó un mensaje. Fue exactamente lo que pasó cuando
    Actions dejó de honrar el cron.

    Solo se avisa de `skipped_stale`, que es lo definitivamente perdido. Un
    `failed` sigue dentro de la ventana y el próximo tick lo reintenta, así que
    avisar de eso sería ruido. Y como `mark_stale` solo devuelve `skipped_stale`
    la primera vez que ve la ocurrencia, cada pérdida avisa una sola vez.
    """
    perdidas = result.of("skipped_stale")
    if not perdidas:
        return

    por_chat: dict[str, list[Outcome]] = {}
    for outcome in perdidas:
        por_chat.setdefault(outcome.reminder.chat_id, []).append(outcome)

    for chat_id, outcomes in por_chat.items():
        lineas = ["⚠️ <b>Esto se perdió: no salió a tiempo</b>", ""]
        for outcome in outcomes:
            cuando = local_str(outcome.occurrence_at, outcome.reminder.timezone)
            lineas.append(f"• {html.escape(outcome.reminder.name)} — era para {cuando}")
        lineas += ["", "El reloj (workflow <code>tick</code>) estuvo caído o muy atrasado."]

        try:
            sender.send_message(chat_id=chat_id, text="\n".join(lineas), parse_mode="HTML")
        except Exception as exc:  # avisar es un extra: no puede tumbar el tick
            result.alert_error = f"{type(exc).__name__}: {exc}"
            return
        result.alerts_sent += 1


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


def lookback_minutes(settings: Settings) -> int:
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
