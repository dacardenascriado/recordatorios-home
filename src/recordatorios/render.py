"""La página: un instrumento para leer de un vistazo, no una landing.

La pregunta que contesta —¿se quedó algo sin salir?— se responde en medio
segundo o no sirve. De ahí las tres decisiones de diseño:

**La tira de latidos manda.** Una marca por ocurrencia esperada, agrupada por
día. Una caída no se lee: se ve, como una racha roja. Es exactamente la forma
que tuvo el apagón de agosto de 2026, y es lo primero que aparece.

**Los problemas van arriba y con acción.** Lo sano se colapsa en la tira; lo que
falló ocupa espacio y trae el botón para mandarlo a mano. Una página de estado
que obliga a buscar el problema en una tabla no está haciendo su trabajo.

**Los datos van en monoespaciada.** Esto es un registro: horas e ids alineados
en columna se comparan de un vistazo, la prosa no. La tipografía dice qué clase
de documento es esto.

Paleta de esmalte y verdigrís —loza de cocina, que es de lo que trata el
sistema— y nada de recursos externos: la página se sirve desde Pages sin
backend, y cada archivo remoto sería una dependencia que se puede caer y una
petición que nadie pidió.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from recordatorios.dashboard import Fila, Resumen

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

CSS = """
:root {
  --ground:#edefee; --surface:#ffffff; --raised:#f7f8f7;
  --ink:#16211f; --ink-soft:#5c6b67; --line:#d8ddda;
  --brand:#1f6f63; --ok:#2e7d5b; --warn:#95620c; --bad:#b3322a;
  --radius:14px;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"Cascadia Code","SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{
    --ground:#101614; --surface:#17201d; --raised:#1d2724;
    --ink:#e6ebe8; --ink-soft:#90a09b; --line:#26322e;
    --brand:#4fb3a0; --ok:#57b98a; --warn:#d9a441; --bad:#e8695c;
  }
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:clamp(1.25rem,4vw,3rem) clamp(1rem,4vw,2rem) 5rem;
  background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.5;
  font-variant-numeric:tabular-nums;
}
main{max-width:60rem; margin:0 auto}

/* --- encabezado ------------------------------------------------------ */
.top{display:flex; flex-wrap:wrap; align-items:baseline; gap:.5rem 1rem; margin-bottom:.35rem}
h1{
  font-size:clamp(1.35rem,4vw,1.75rem); font-weight:700; letter-spacing:-.025em;
  margin:0; font-family:var(--mono);
}
.veredicto{
  font-size:clamp(1.35rem,4vw,1.75rem); font-weight:700; letter-spacing:-.02em;
  margin-left:auto;
}
.veredicto.bien{color:var(--ok)} .veredicto.mal{color:var(--bad)}
.resumen{color:var(--ink-soft); font-size:.9375rem; margin:0 0 2rem}
.resumen b{color:var(--ink); font-weight:600}

/* --- la firma: tira de latidos --------------------------------------- */
.tira{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:1.15rem 1.25rem 1rem; overflow-x:auto; margin-bottom:1.5rem;
}
.dias{display:flex; gap:clamp(.5rem,2.5vw,1.35rem); min-width:min-content}
.dia{display:flex; flex-direction:column; gap:.5rem; flex:0 0 auto}
.dia-nombre{
  font-size:.6875rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-soft); font-family:var(--mono); white-space:nowrap;
}
.marcas{display:flex; gap:4px}
.marca{
  width:13px; height:26px; border-radius:3px; background:var(--line);
  border:0; padding:0; display:block;
}
.marca.enviado{background:var(--ok)}
.marca.en-curso{background:var(--warn)}
.marca.perdido,.marca.vencido,.marca.fallido{background:var(--bad)}
.leyenda{
  display:flex; flex-wrap:wrap; gap:.4rem 1.1rem; margin:.9rem 0 0;
  font-size:.75rem; color:var(--ink-soft);
}
.leyenda span{display:flex; align-items:center; gap:.4rem; white-space:nowrap}
.punto{width:9px; height:9px; border-radius:2px; flex:0 0 auto}

/* --- tarjetas de problema -------------------------------------------- */
h2{
  font-size:.75rem; letter-spacing:.1em; text-transform:uppercase; font-weight:600;
  color:var(--ink-soft); margin:2.5rem 0 .85rem; font-family:var(--mono);
}
.problema{
  background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--bad); border-radius:var(--radius);
  padding:1rem 1.15rem; margin-bottom:.65rem;
}
.problema-top{display:flex; flex-wrap:wrap; align-items:center; gap:.5rem .75rem}
.rid{font-family:var(--mono); font-size:.9375rem; font-weight:600; word-break:break-all}
.cuando{color:var(--ink-soft); font-size:.875rem; font-family:var(--mono)}
.motivo{color:var(--ink-soft); font-size:.8125rem; margin:.45rem 0 0}
.acciones{display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.9rem}
.btn{
  font:inherit; font-size:.875rem; font-weight:500; cursor:pointer;
  padding:.45rem .9rem; border-radius:8px; border:1px solid var(--line);
  background:var(--raised); color:var(--ink); text-decoration:none;
  display:inline-flex; align-items:center; gap:.4rem; transition:background .12s,border-color .12s;
}
.btn:hover{border-color:var(--brand)}
.btn-primario{background:var(--brand); border-color:var(--brand); color:#fff}
@media (prefers-color-scheme:dark){ .btn-primario{color:#0d1412} }
.btn-primario:hover{filter:brightness(1.08)}
.btn:focus-visible,.marca:focus-visible{outline:2px solid var(--brand); outline-offset:2px}
.pista{font-size:.8125rem; color:var(--ink-soft); margin:.7rem 0 0}
.sano{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:1.1rem 1.25rem; color:var(--ink-soft); font-size:.9375rem;
}

/* --- tablas ----------------------------------------------------------- */
.tabla{background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); overflow:hidden}
table{width:100%; border-collapse:collapse; font-size:.875rem}
th{
  text-align:left; font-size:.6875rem; letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-soft); font-weight:600; padding:.6rem 1rem; border-bottom:1px solid var(--line);
}
td{padding:.55rem 1rem; border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
td.t,td.id{font-family:var(--mono); white-space:nowrap}
.etiqueta{font-size:.75rem; font-weight:600}
.etiqueta.enviado{color:var(--ok)}
.etiqueta.en-curso{color:var(--warn)}
.etiqueta.perdido,.etiqueta.vencido,.etiqueta.fallido{color:var(--bad)}

/* Bajo 640px una tabla obliga a hacer scroll lateral para leer una fila.
   Cada fila pasa a ser una ficha: se lee de arriba abajo, como el pulgar. */
@media (max-width:640px){
  .tabla thead{position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0)}
  .tabla tr{display:block; padding:.7rem 1rem; border-bottom:1px solid var(--line)}
  .tabla tr:last-child{border-bottom:0}
  .tabla td{display:flex; justify-content:space-between; gap:1rem; padding:.15rem 0; border:0}
  .tabla td::before{content:attr(data-l); color:var(--ink-soft); font-size:.75rem; font-family:var(--sans)}
  .tabla td:empty{display:none}
}

footer{margin-top:3rem; color:var(--ink-soft); font-size:.8125rem; line-height:1.6}
footer code{font-family:var(--mono); font-size:.75rem}
@media (prefers-reduced-motion:reduce){ *{transition:none!important; animation:none!important} }
"""

JS = """
document.querySelectorAll('[data-copiar]').forEach(function(b){
  b.addEventListener('click', function(){
    var texto = b.getAttribute('data-copiar');
    var previo = b.textContent;
    function hecho(){ b.textContent = 'copiado'; setTimeout(function(){ b.textContent = previo; }, 1400); }
    if (navigator.clipboard) { navigator.clipboard.writeText(texto).then(hecho, function(){}); }
  });
});
"""


def render(resumen: Resumen, tz_name: str = "America/Bogota", repo: str | None = None) -> str:
    """Arma la página. `repo` ('usuario/repo') habilita los botones de reenvío."""
    tz = ZoneInfo(tz_name)

    def hora(m: datetime | None) -> str:
        return m.astimezone(tz).strftime("%d %b %H:%M") if m else "—"

    problemas = resumen.problemas
    veredicto = "mal" if problemas else "bien"

    return (
        "<!doctype html>\n"
        '<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        '<meta name="color-scheme" content="light dark">\n'
        "<title>recordatorios-home — estado</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        f"{_encabezado(resumen, problemas, veredicto)}"
        f"{_tira(resumen, tz)}"
        f"{_problemas(problemas, hora, repo)}"
        f"{_historia(resumen, hora)}"
        f"{_futuro(resumen, hora)}"
        f"{_pie(tz_name, resumen, hora)}"
        "</main>\n"
        # El único JS de la página es el de copiar el id. Sin botones no hay
        # nada que ejecutar, así que no se emite.
        + (f"<script>{JS}</script>\n" if repo and problemas else "")
        + "</body>\n</html>\n"
    )


def _encabezado(resumen: Resumen, problemas: list[Fila], veredicto: str) -> str:
    if problemas:
        n = len(problemas)
        frase = f"<b>{n}</b> {'recordatorio' if n == 1 else 'recordatorios'} sin salir"
    else:
        frase = "<b>todos</b> salieron"
    return (
        '  <div class="top">\n'
        "    <h1>recordatorios-home</h1>\n"
        f'    <span class="veredicto {veredicto}">{resumen.salud}</span>\n'
        "  </div>\n"
        f'  <p class="resumen">De <b>{len(resumen.pasado)}</b> programados en 7 días, {frase}.</p>\n'
    )


def _tira(resumen: Resumen, tz: ZoneInfo) -> str:
    """Una marca por ocurrencia, agrupada por día. La caída se ve, no se lee."""
    if not resumen.pasado:
        return ""

    por_dia: dict[str, list[Fila]] = defaultdict(list)
    for fila in sorted(resumen.pasado, key=lambda f: f.occurrence_at):
        local = fila.occurrence_at.astimezone(tz)
        por_dia[f"{DIAS[local.weekday()]} {local.day}"].append(fila)

    dias = "\n".join(
        f'      <div class="dia"><span class="dia-nombre">{html.escape(etiqueta)}</span>'
        f'<div class="marcas">'
        + "".join(
            f'<span class="marca {_cls(f.estado)}" title="{html.escape(f.reminder_id)} · '
            f'{f.occurrence_at.astimezone(tz).strftime("%H:%M")} · {f.estado}"></span>'
            for f in filas
        )
        + "</div></div>"
        for etiqueta, filas in por_dia.items()
    )

    leyenda = "".join(
        f'<span><i class="punto" style="background:var(--{v})"></i>{t}</span>'
        for t, v in [("salió", "ok"), ("en camino", "warn"), ("no salió", "bad")]
    )
    return (
        f'  <div class="tira">\n    <div class="dias">\n{dias}\n    </div>\n'
        f'    <p class="leyenda">{leyenda}</p>\n  </div>\n'
    )


def _problemas(problemas: list[Fila], hora, repo: str | None) -> str:
    if not problemas:
        return (
            "  <h2>Sin salir</h2>\n"
            '  <div class="sano">Nada se quedó sin salir en los últimos 7 días.</div>\n'
        )

    tarjetas = []
    for f in problemas:
        acciones = (
            f'      <div class="acciones">\n'
            f'        <a class="btn btn-primario" target="_blank" rel="noopener"'
            f' href="https://github.com/{html.escape(repo)}/actions/workflows/tick.yml">'
            f"Mandarlo ahora ↗</a>\n"
            f'        <button type="button" class="btn" data-copiar="{html.escape(f.reminder_id)}">'
            f"Copiar id</button>\n      </div>\n"
            f'      <p class="pista">Abre Actions. Elegí <code>send-test</code>, pegá el id '
            f"y ejecutá.</p>\n"
            if repo
            else ""
        )
        tarjetas.append(
            f'  <div class="problema">\n    <div class="problema-top">\n'
            f'      <span class="rid">{html.escape(f.reminder_id)}</span>\n'
            f'      <span class="cuando">{hora(f.occurrence_at)}</span>\n'
            f'      <span class="etiqueta {_cls(f.estado)}">{f.estado}</span>\n'
            f"    </div>\n"
            f'    <p class="motivo">{html.escape(f.detalle or "")}</p>\n'
            f"{acciones}  </div>\n"
        )
    return "  <h2>Sin salir</h2>\n" + "".join(tarjetas)


def _historia(resumen: Resumen, hora) -> str:
    if not resumen.pasado:
        return ""
    filas = "\n".join(
        f'      <tr><td class="t" data-l="Programado">{hora(f.occurrence_at)}</td>'
        f'<td class="id" data-l="Recordatorio">{html.escape(f.reminder_id)}</td>'
        f'<td data-l="Estado"><span class="etiqueta {_cls(f.estado)}">{f.estado}</span></td>'
        f'<td class="t" data-l="Salió">{hora(f.entregado_at)}</td></tr>'
        for f in resumen.pasado
    )
    return (
        "  <h2>Últimos 7 días</h2>\n"
        '  <div class="tabla"><table><thead><tr><th>Programado</th><th>Recordatorio</th>'
        f"<th>Estado</th><th>Salió</th></tr></thead><tbody>\n{filas}\n"
        "    </tbody></table></div>\n"
    )


def _futuro(resumen: Resumen, hora) -> str:
    if not resumen.futuro:
        return ""
    filas = "\n".join(
        f'      <tr><td class="t" data-l="Programado">{hora(cuando)}</td>'
        f'<td class="id" data-l="Recordatorio">{html.escape(rid)}</td></tr>'
        for rid, cuando in resumen.futuro
    )
    return (
        "  <h2>Próximos 14 días</h2>\n"
        '  <div class="tabla"><table><thead><tr><th>Programado</th>'
        f"<th>Recordatorio</th></tr></thead><tbody>\n{filas}\n"
        "    </tbody></table></div>\n"
    )


def _pie(tz_name: str, resumen: Resumen, hora) -> str:
    return (
        "  <footer>\n"
        f"    Horas en {html.escape(tz_name)}. Página generada el "
        f"{hora(resumen.generado_at)}, al terminar un bloque del reloj; si quedó muchas "
        f"horas atrás, el reloj está caído.<br>\n"
        f"    <b>perdido</b>: ningún tick llegó a ver la ocurrencia. "
        f"<b>vencido</b>: la vio, pero ya había pasado su <code>max_delay_minutes</code>. "
        f"<b>fallido</b>: Telegram la rechazó y se reintenta solo.<br>\n"
        f"    Sin nombres: el repo es público.\n"
        "  </footer>\n"
    )


def _cls(estado: str) -> str:
    """`en curso` -> `en-curso`: los estados con espacio no sirven como clase CSS."""
    return estado.replace(" ", "-")
