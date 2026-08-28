"""Modelo de un recordatorio."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date

# Lunes. Sirve de "semana 0" para el conteo de every_weeks/week_offset.
DEFAULT_ANCHOR = date(1970, 1, 5)

DEFAULT_TIMEZONE = "America/Bogota"

# Cuánto se tolera entregar tarde. Tiene que quedar por debajo de la ventana de
# recuperación (TICK_LOOKBACK_MINUTES) para que lo vencido todavía se vea y se
# pueda registrar; el margen entre los dos es lo que hace visible una pérdida.
# Bajarlo por recordatorio tiene sentido cuando recibirlo tarde no sirve de nada.
DEFAULT_MAX_DELAY_MINUTES = 120

TURNO = "{turno}"
SIGUIENTE = "{siguiente}"

_ETIQUETA = re.compile(r"<[^>]+>")


def plain(text: str) -> str:
    """El texto sin formato, que es lo único que acepta una encuesta.

    La pregunta de un `sendPoll` no interpreta HTML: un `<b>` llegaría
    literal. Así que las negritas se caen, pero el texto se conserva.
    """
    return html.unescape(_ETIQUETA.sub("", text))


@dataclass(frozen=True)
class Reminder:
    """Un recordatorio: cuándo dispara y qué manda a Telegram.

    El horario es `cron` evaluado en `timezone`. Sobre esas ocurrencias se
    aplica el filtro de semanas: solo dispara cuando la semana de la ocurrencia
    cumple `semana % every_weeks == week_offset`, contando semanas desde
    `anchor`. Con every_weeks=2, un recordatorio con week_offset=0 y otro con
    week_offset=1 se alternan sin solaparse nunca.

    Si hay `rotation`, cada día en que el recordatorio dispara le corresponde a
    la siguiente persona de la lista, empezando por `anchor`. Y si `messages`
    trae varios textos, se van turnando con el mismo índice: entre una lista de
    3 personas y una de 5 mensajes pasan 15 turnos antes de repetirse la misma
    combinación.
    """

    id: str
    name: str
    cron: str
    messages: tuple[str, ...]
    chat_id: str
    timezone: str = DEFAULT_TIMEZONE
    enabled: bool = True
    every_weeks: int = 1
    week_offset: int = 0
    anchor: date = DEFAULT_ANCHOR
    rotation: tuple[str, ...] = ()
    starts_on: date | None = None
    ends_on: date | None = None
    max_delay_minutes: int = DEFAULT_MAX_DELAY_MINUTES
    parse_mode: str | None = "HTML"
    silent: bool = False
    poll_options: tuple[str, ...] = ()

    @property
    def is_poll(self) -> bool:
        """¿Se manda como encuesta en vez de como mensaje?"""
        return bool(self.poll_options)

    @property
    def label(self) -> str:
        return f"{self.id} ({self.name})" if self.name != self.id else self.id

    @property
    def needs_turn(self) -> bool:
        """¿Hace falta calcular el número de turno para armar el mensaje?

        Si no, nos ahorramos el recorrido del calendario desde el anchor.
        """
        return bool(self.rotation) or len(self.messages) > 1

    def whose_turn(self, turn: int) -> str | None:
        if not self.rotation:
            return None
        return self.rotation[turn % len(self.rotation)]

    def render(self, turn: int = 0) -> str:
        """Arma el texto del turno indicado, con los nombres ya reemplazados."""
        texto = self.messages[turn % len(self.messages)]
        if self.rotation:
            siguiente = self.rotation[(turn + 1) % len(self.rotation)]
            texto = texto.replace(TURNO, self.rotation[turn % len(self.rotation)])
            texto = texto.replace(SIGUIENTE, siguiente)
        return texto

    def render_question(self, turn: int = 0) -> str:
        """El mismo texto del turno, pero servible como pregunta de encuesta."""
        return plain(self.render(turn))
