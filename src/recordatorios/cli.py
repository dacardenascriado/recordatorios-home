"""Interfaz de línea de comandos: python -m recordatorios <comando>."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from recordatorios.config import ConfigError, Settings, load_dotenv
from recordatorios.loader import load_reminders
from recordatorios.models import Reminder
from recordatorios.schedule import (
    describe,
    local_str,
    nearest_run,
    next_runs,
    occurrences_between,
    turn_index,
)
from recordatorios.store import Store
from recordatorios.telegram import TelegramError, TelegramSender
from recordatorios.tick import lookback_minutes, run_tick


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    load_dotenv()
    try:
        settings = Settings.from_env()
        return args.handler(args, settings)
    except ConfigError as exc:
        print(f"\nError de configuración:\n{exc.report()}", file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"\nError de Telegram: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recordatorios",
        description="Recordatorios recurrentes por Telegram definidos en reminders.yaml",
    )
    subs = parser.add_subparsers(dest="comando", required=True)

    validar = subs.add_parser("validate", help="Revisa que reminders.yaml esté bien escrito")
    validar.set_defaults(handler=cmd_validate)

    check = subs.add_parser(
        "check", help="Comprueba de punta a punta: YAML, token, chats y base de datos"
    )
    check.set_defaults(handler=cmd_check)

    listar = subs.add_parser("list", help="Lista los recordatorios y su próxima ejecución")
    listar.set_defaults(handler=cmd_list)

    agenda = subs.add_parser("agenda", help="Cronología combinada de las próximas ejecuciones")
    agenda.add_argument("--days", type=int, default=28, help="Días hacia adelante (por defecto 28)")
    agenda.add_argument("--id", help="Filtra por id de recordatorio")
    agenda.set_defaults(handler=cmd_agenda)

    tick = subs.add_parser("tick", help="Envía lo que haya vencido desde el último tick")
    tick.add_argument("--dry-run", action="store_true", help="Muestra qué haría, sin enviar nada")
    tick.set_defaults(handler=cmd_tick)

    prueba = subs.add_parser("send-test", help="Envía un recordatorio ahora mismo, a mano")
    prueba.add_argument("--id", required=True, help="Id del recordatorio")
    prueba.set_defaults(handler=cmd_send_test)

    initdb = subs.add_parser("init-db", help="Crea las tablas en la base de datos")
    initdb.set_defaults(handler=cmd_init_db)

    historial = subs.add_parser("history", help="Últimos envíos registrados")
    historial.add_argument("--limit", type=int, default=20)
    historial.set_defaults(handler=cmd_history)

    return parser


# -- comandos -------------------------------------------------------------


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    recordatorios = load_reminders(settings.reminders_file)
    activos = sum(1 for r in recordatorios if r.enabled)
    print(
        f"OK: {settings.reminders_file} define {len(recordatorios)} recordatorio(s), "
        f"{activos} activo(s)."
    )
    for aviso in _warnings(recordatorios, settings):
        print(f"  aviso: {aviso}")
    return 0


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    """Revisa las cuatro cosas que tienen que estar bien para que llegue un mensaje.

    No envía nada: `getChat` pregunta por el chat sin escribir en él.
    """
    fallos = 0

    try:
        recordatorios = load_reminders(settings.reminders_file)
    except ConfigError as exc:
        print(f"[MAL] reminders.yaml\n{exc.report()}", file=sys.stderr)
        print("\nSin un YAML válido no puedo revisar lo demás.", file=sys.stderr)
        return 1

    activos = [r for r in recordatorios if r.enabled]
    print(f"[OK ] reminders.yaml — {len(recordatorios)} recordatorios, {len(activos)} activos")

    sender = None
    try:
        sender = TelegramSender(settings.require_token())
        bot = sender.get_me()
        print(f"[OK ] Token de Telegram — el bot es @{bot.get('username', '?')}")
    except (ConfigError, TelegramError) as exc:
        print(f"[MAL] Token de Telegram — {exc}", file=sys.stderr)
        fallos += 1

    if sender is not None:
        for chat_id in sorted({r.chat_id for r in activos}):
            visible = _oculto(chat_id)
            try:
                chat = sender.get_chat(chat_id)
                nombre = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
                print(f"[OK ] Chat {visible} — alcanzable ({chat.get('type', '?')}: {nombre})")
            except TelegramError as exc:
                print(f"[MAL] Chat {visible} — {exc}", file=sys.stderr)
                fallos += 1

    try:
        with Store.open(settings.database_url) as store:
            store.init_schema()
            enviados = len(store.history(limit=1))
        print(f"[OK ] Base de datos ({store.backend}) — conecta y el esquema está listo")
        if not settings.database_url:
            print(
                "       aviso: sin DATABASE_URL se usa un SQLite local. En GitHub "
                "Actions eso se pierde entre corridas y los mensajes se duplicarían."
            )
        elif enviados == 0:
            print("       (todavía sin envíos registrados, es normal antes del primero)")
    except Exception as exc:
        print(f"[MAL] Base de datos — {type(exc).__name__}: {exc}", file=sys.stderr)
        fallos += 1

    print()
    if fallos:
        print(f"{fallos} problema(s). Los mensajes NO van a llegar hasta resolverlos.")
        return 1

    # Con key explícita: si dos coinciden en el instante, ordenar tuplas
    # intentaría comparar los Reminder entre sí y eso revienta.
    proximas = sorted(
        ((momento, r) for r in activos for momento in next_runs(r, 1)),
        key=lambda item: (item[0], item[1].id),
    )
    print("Todo en orden. Lo próximo que va a pasar:")
    for momento, reminder in proximas[:5]:
        turno = turn_index(reminder, momento) if reminder.needs_turn else 0
        quien = reminder.whose_turn(turno)
        print(
            f"  {local_str(momento, reminder.timezone):<32} {reminder.id}"
            + (f"  →  {quien}" if quien else "")
        )
    return 0


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    recordatorios = load_reminders(settings.reminders_file)
    if not recordatorios:
        print("No hay recordatorios definidos.")
        return 0

    for reminder in recordatorios:
        estado = "activo" if reminder.enabled else "PAUSADO"
        print(f"\n{reminder.id}  [{estado}]  {reminder.name}")
        print(f"  horario : {describe(reminder)}")
        for i, texto in enumerate(reminder.messages):
            etiqueta = "mensaje " if len(reminder.messages) == 1 else f"msg [{i}] "
            print(f"  {etiqueta}: {_preview(texto)}")
        if reminder.enabled:
            proximas = next_runs(reminder, count=3)
            if proximas:
                for momento in proximas:
                    print(f"  próxima : {local_str(momento, reminder.timezone)}")
            else:
                print("  próxima : ninguna (revisa ends_on o el filtro de semanas)")
    return 0


def cmd_agenda(args: argparse.Namespace, settings: Settings) -> int:
    recordatorios = [r for r in load_reminders(settings.reminders_file) if r.enabled]
    if args.id:
        recordatorios = [r for r in recordatorios if r.id == args.id]
        if not recordatorios:
            print(f"No existe un recordatorio activo con id {args.id!r}", file=sys.stderr)
            return 1

    ahora = datetime.now(timezone.utc)
    hasta = ahora + timedelta(days=args.days)

    eventos: list[tuple[datetime, Reminder]] = []
    for reminder in recordatorios:
        for momento in occurrences_between(reminder, ahora, hasta):
            eventos.append((momento, reminder))
    eventos.sort(key=lambda item: (item[0], item[1].id))

    print(f"Próximos {args.days} días — {len(eventos)} ejecución(es)\n")
    for momento, reminder in eventos:
        turno = turn_index(reminder, momento) if reminder.needs_turn else 0
        quien = reminder.whose_turn(turno)
        columna = f"{reminder.id}" + (f"  →  {quien}" if quien else "")
        print(f"  {local_str(momento, reminder.timezone):<32} {columna}")
    if not eventos:
        print("  (nada programado en ese rango)")
    return 0


def cmd_tick(args: argparse.Namespace, settings: Settings) -> int:
    recordatorios = load_reminders(settings.reminders_file)

    sender = _NullSender() if args.dry_run else TelegramSender(settings.require_token())

    # El store no conecta hasta que alguien lo consulta, y run_tick solo lo
    # consulta si hay algo que enviar.
    with Store.open(settings.database_url) as store:
        resultado = run_tick(recordatorios, store, sender, settings, dry_run=args.dry_run)
        destino = store.backend if store.connected else f"{store.backend} (sin conectar)"

    print(f"Backend: {destino}")
    print(resultado.report())
    return 1 if resultado.failures else 0


def cmd_send_test(args: argparse.Namespace, settings: Settings) -> int:
    recordatorios = {r.id: r for r in load_reminders(settings.reminders_file)}
    reminder = recordatorios.get(args.id)
    if reminder is None:
        print(
            f"No existe el recordatorio {args.id!r}. Disponibles: "
            f"{', '.join(sorted(recordatorios)) or '(ninguno)'}",
            file=sys.stderr,
        )
        return 1

    # El turno de la ocurrencia más cercana: la de hoy si ya disparó, y si no
    # la que viene. Mirar solo hacia adelante hacía que una prueba por la tarde
    # nombrara a la persona de la vez siguiente, no a la de hoy.
    momento = nearest_run(reminder)
    turno = turn_index(reminder, momento) if momento and reminder.needs_turn else 0

    sender = TelegramSender(settings.require_token())
    sender.send_message(
        chat_id=reminder.chat_id,
        text=reminder.render(turno),
        parse_mode=reminder.parse_mode,
        silent=reminder.silent,
    )
    # Decir qué ocurrencia se imitó: es la forma de notar a tiempo que el
    # mensaje salió con el turno de otro día.
    referencia = (
        local_str(momento, reminder.timezone) if momento else "sin ocurrencia de referencia"
    )
    quien = reminder.whose_turn(turno)
    print(
        f"Enviado '{reminder.id}' a {reminder.chat_id} — como la ocurrencia "
        f"del {referencia}" + (f", turno de {quien}." if quien else ".")
    )
    return 0


def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    with Store.open(settings.database_url) as store:
        store.init_schema()
        print(f"Esquema listo en backend '{store.backend}'.")
    return 0


def cmd_history(args: argparse.Namespace, settings: Settings) -> int:
    with Store.open(settings.database_url) as store:
        store.init_schema()
        filas = store.history(limit=args.limit)
    if not filas:
        print("Todavía no hay envíos registrados.")
        return 0
    for fila in filas:
        cuando = fila.logged_at.strftime("%Y-%m-%d %H:%M:%SZ")
        detalle = f" — {fila.detail}" if fila.detail else ""
        print(f"  {cuando}  {fila.status:<14} {fila.reminder_id}{detalle}")
    return 0


# -- utilidades -----------------------------------------------------------


class _NullSender:
    """Emisor que no hace nada, para --dry-run."""

    def send_message(self, chat_id: str, text: str, parse_mode=None, silent=False) -> dict:
        return {}


def _warnings(recordatorios: list[Reminder], settings: Settings | None = None) -> list[str]:
    """Cosas legales pero probablemente no deseadas."""
    avisos: list[str] = []

    # Un max_delay que llegue o pase la ventana de recuperación devuelve el
    # sistema a su peor modo de fallo: lo que se vence ya salió de la ventana
    # cuando el tick lo miraría, así que se pierde sin fila en la base, sin
    # línea en el log y sin aviso. Vale la pena gritarlo acá y no descubrirlo
    # el día que falte un recordatorio.
    if settings is not None:
        ventana = lookback_minutes(settings)
        for reminder in recordatorios:
            if reminder.enabled and reminder.max_delay_minutes >= ventana:
                avisos.append(
                    f"{reminder.id}: max_delay_minutes ({reminder.max_delay_minutes}) "
                    f"no es menor que la ventana de recuperación ({ventana} min); "
                    f"una pérdida ahí no dejaría rastro"
                )
    for reminder in recordatorios:
        if reminder.enabled and not next_runs(reminder, count=1):
            avisos.append(f"{reminder.id}: no tiene ninguna ejecución futura")

    # Dos recordatorios idénticos en horario y semana mandan mensajes duplicados.
    vistos: dict[tuple, str] = {}
    for reminder in recordatorios:
        if not reminder.enabled:
            continue
        clave = (
            reminder.cron,
            reminder.timezone,
            reminder.every_weeks,
            reminder.week_offset,
            reminder.chat_id,
        )
        if clave in vistos:
            avisos.append(
                f"{reminder.id} y {vistos[clave]} disparan exactamente al mismo tiempo "
                f"al mismo chat"
            )
        else:
            vistos[clave] = reminder.id
    return avisos


def _oculto(chat_id: str) -> str:
    """Deja ver solo el final del chat_id.

    No es una credencial —sin el token no sirve de nada— pero es un
    identificador, y este log queda público en un repo público.
    """
    return f"…{chat_id[-4:]}" if len(chat_id) > 4 else "…"


def _preview(texto: str, largo: int = 60) -> str:
    plano = " ".join(texto.split())
    return plano if len(plano) <= largo else plano[: largo - 1] + "…"
