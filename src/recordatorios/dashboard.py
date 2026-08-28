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


# -- render ----------------------------------------------------------------


def render(resumen: Resumen, tz_name: str = "America/Bogota") -> str:
    """Una sola página, sin recursos externos ni JavaScript.

    Se sirve desde GitHub Pages, así que no hay backend que consultar: todo lo
    que se ve acá se calculó en el runner, que sí tiene los secrets.
    """
    tz = ZoneInfo(tz_name)

    def hora(moment: datetime | None) -> str:
        return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") if moment else "—"

    problemas = resumen.problemas
    clase_salud = "mal" if problemas else "bien"

    filas_pasado = "\n".join(
        f"""      <tr class="{f.estado.replace(" ", "-")}">
        <td>{hora(f.occurrence_at)}</td>
        <td><code>{html.escape(f.reminder_id)}</code></td>
        <td><span class="pill {f.estado.replace(" ", "-")}">{f.estado}</span></td>
        <td>{hora(f.entregado_at)}</td>
        <td class="detalle">{html.escape(f.detalle or "")}</td>
      </tr>"""
        for f in resumen.pasado
    )

    filas_futuro = "\n".join(
        f"""      <tr><td>{hora(cuando)}</td><td><code>{html.escape(rid)}</code></td></tr>"""
        for rid, cuando in resumen.futuro
    )

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>recordatorios-home — estado</title>
<style>
  :root {{
    --fondo: #fbfbfa; --texto: #1a1a1a; --suave: #6b6b6b; --linea: #e4e4e1;
    --tarjeta: #ffffff; --bien: #1a7f4b; --mal: #b3261e; --tibio: #8a6100;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --fondo: #16161a; --texto: #ececec; --suave: #9a9a9a; --linea: #2c2c31;
      --tarjeta: #1d1d22; --bien: #4ec98a; --mal: #ff6b5e; --tibio: #e0a93c;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1.25rem 4rem; background: var(--fondo); color: var(--texto);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  main {{ max-width: 62rem; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; letter-spacing: -.01em; }}
  h2 {{ font-size: 1rem; margin: 2.5rem 0 .75rem; letter-spacing: -.01em; }}
  .sub {{ color: var(--suave); margin: 0 0 1.5rem; font-size: .875rem; }}
  .estado {{
    display: flex; flex-wrap: wrap; gap: 1.5rem; padding: 1rem 1.25rem;
    background: var(--tarjeta); border: 1px solid var(--linea); border-radius: 10px;
  }}
  .estado div {{ min-width: 9rem; }}
  .estado dt {{ color: var(--suave); font-size: .75rem; text-transform: uppercase;
                letter-spacing: .06em; margin-bottom: .2rem; }}
  .estado dd {{ margin: 0; font-size: 1.05rem; font-variant-numeric: tabular-nums; }}
  .bien {{ color: var(--bien); }}
  .mal {{ color: var(--mal); }}
  .tabla-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .875rem; }}
  th {{ text-align: left; font-weight: 600; color: var(--suave); font-size: .75rem;
        text-transform: uppercase; letter-spacing: .06em;
        padding: .4rem .6rem; border-bottom: 1px solid var(--linea); }}
  td {{ padding: .45rem .6rem; border-bottom: 1px solid var(--linea);
        font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.detalle {{ white-space: normal; color: var(--suave); font-size: .8125rem; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8125rem; }}
  .pill {{ display: inline-block; padding: .05rem .5rem; border-radius: 999px;
           font-size: .75rem; border: 1px solid var(--linea); }}
  .pill.enviado {{ color: var(--bien); border-color: currentColor; }}
  .pill.perdido, .pill.vencido, .pill.fallido {{ color: var(--mal); border-color: currentColor; }}
  .pill.en-curso {{ color: var(--tibio); border-color: currentColor; }}
  footer {{ margin-top: 3rem; color: var(--suave); font-size: .8125rem; }}
  .vacio {{ color: var(--suave); font-style: italic; }}
</style>
</head>
<body>
<main>
  <h1>recordatorios-home</h1>
  <p class="sub">Lo que el calendario decía que tenía que salir, contra lo que
  salió de verdad. Sin nombres: el repo es público.</p>

  <dl class="estado">
    <div>
      <dt>Estado</dt>
      <dd class="{clase_salud}">{resumen.salud}</dd>
    </div>
    <div>
      <dt>Problemas (7 días)</dt>
      <dd class="{clase_salud}">{len(problemas)}</dd>
    </div>
    <div>
      <dt>Último envío</dt>
      <dd>{hora(resumen.ultima_entrega)}</dd>
    </div>
    <div>
      <dt>Página generada</dt>
      <dd>{hora(resumen.generado_at)}</dd>
    </div>
  </dl>

  <h2>Últimos 7 días</h2>
  <div class="tabla-wrap">
  {"<table><thead><tr><th>Programado</th><th>Recordatorio</th><th>Estado</th>"
   "<th>Salió</th><th>Detalle</th></tr></thead><tbody>"
   + filas_pasado + "</tbody></table>" if resumen.pasado
   else '<p class="vacio">Nada programado en este rango.</p>'}
  </div>

  <h2>Próximos 14 días</h2>
  <div class="tabla-wrap">
  {"<table><thead><tr><th>Programado</th><th>Recordatorio</th></tr></thead><tbody>"
   + filas_futuro + "</tbody></table>" if resumen.futuro
   else '<p class="vacio">Nada programado.</p>'}
  </div>

  <footer>
    Horas en {html.escape(tz_name)}. <strong>perdido</strong> significa que ningún
    tick llegó a ver la ocurrencia; <strong>vencido</strong>, que la vio pero ya
    había pasado su <code>max_delay_minutes</code> y no se envió.
    Si «Página generada» quedó muy atrás, el reloj está caído.
  </footer>
</main>
</body>
</html>
"""
