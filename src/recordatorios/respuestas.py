"""Lo que alguien contestó en una encuesta, contado al resto del grupo.

Una encuesta de Telegram le avisa a quien la mandó —el bot—, no a las personas
del chat. Quien marca "hoy no puedo" queda tranquilo creyendo que ya avisó, y
los demás no ven absolutamente nada salvo que abran la encuesta a mirar los
votos. Justo la novedad más importante que manda este sistema era la única que
llegaba en silencio.

Acá se cierra ese lazo: se leen las respuestas con `getUpdates` y cada una se
convierte en un mensaje en el mismo chat, colgado de la encuesta que la
originó. El grupo se entera igual que se entera de todo lo demás.

Tres cosas sostienen el diseño:

**No hay cursor que guardar.** `getUpdates(offset=N)` pide desde N y, con eso
mismo, confirma lo anterior: Telegram lo borra y no vuelve a mandarlo. El
cursor vive en Telegram, así que leer las respuestas no obliga a despertar la
base — y como casi ninguna corrida encuentra votos, casi ninguna la abre.

**Solo se confirma lo que ya se avisó.** Si un aviso no sale, ahí se corta: lo
que quedó atrás sigue pendiente en Telegram (24 h) y lo retoma la corrida
siguiente. Una respuesta no puede desaparecer sin que nadie la haya visto.

**Solo se avisan encuestas registradas.** La primera lectura se trae hasta 24 h
de votos acumulados; anunciarlos todos de golpe sería un aluvión en el chat por
cosas que ya pasaron. Como la tabla `polls` solo tiene las encuestas enviadas
desde que existe este módulo, ese arrastre se descarta solo.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone

from recordatorios.models import Reminder
from recordatorios.store import Store
from recordatorios.telegram import Bot

# Solo interesan los votos. Pedirlo explícitamente hace que Telegram no mande
# lo demás (mensajes del grupo, ediciones, entradas y salidas): menos que
# recorrer, y ninguna conversación de la casa pasando por acá sin necesidad.
TIPOS = ("poll_answer",)


@dataclass
class RespuestasResult:
    """El informe de la corrida. Sin nombres: este log es público."""

    leidas: int = 0
    avisos: int = 0
    repetidas: int = 0
    desconocidas: int = 0
    confirmado: int | None = None
    error: str | None = None
    dry_run: bool = False
    touched_database: bool = False
    lineas: list[str] = field(default_factory=list)

    def report(self) -> str:
        modo = " (simulación)" if self.dry_run else ""
        cabeza = f"Respuestas{modo}: {self.leidas} actualización(es) leída(s)"
        if not self.leidas:
            return f"{cabeza}\nNada nuevo. No se abrió conexión a la base."

        lineas = [cabeza]
        lineas.extend(f"  {linea}" for linea in self.lineas)
        lineas.append(
            f"Resumen: {self.avisos} aviso(s), {self.repetidas} repetida(s), "
            f"{self.desconocidas} sin encuesta conocida."
        )
        if self.confirmado is not None:
            lineas.append(f"Leído y confirmado hasta el update {self.confirmado}.")
        if self.error:
            lineas.append(f"Problema: {self.error}")
        return "\n".join(lineas)


def run_respuestas(
    reminders: list[Reminder],
    store: Store,
    bot: Bot,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RespuestasResult:
    """Lee los votos pendientes y los avisa al chat. `store` se conecta perezoso:
    sin votos que resolver, esta función no lo usa y nunca llega a abrirse.

    En `dry_run` sí lee la base (hace falta para saber de qué encuesta es cada
    voto), pero no manda nada, no registra nada y no confirma la lectura: lo
    mismo se puede volver a correr de verdad después.
    """
    now = now or datetime.now(timezone.utc)
    result = RespuestasResult(dry_run=dry_run)

    updates = bot.get_updates(allowed_updates=list(TIPOS))
    result.leidas = len(updates)
    if not updates:
        return result

    if not any(isinstance(u.get("poll_answer"), dict) for u in updates):
        # `allowed_updates` no afecta a lo que Telegram ya tenía encolado, así
        # que las primeras lecturas pueden traer cosas que no nos importan.
        # Se confirman para que no vuelvan, y listo.
        result.lineas.append("nada que sean votos; se confirma y se sigue")
        _confirmar(bot, updates[-1].get("update_id"), result, dry_run)
        return result

    nombres = {r.id: r.name for r in reminders}
    result.touched_database = True
    store.init_schema()

    confirmable: int | None = None
    for update in updates:
        answer = update.get("poll_answer")
        if isinstance(answer, dict):
            if not _anunciar(answer, store, bot, nombres, now, result, dry_run):
                break  # se corta acá: lo que sigue queda para la próxima corrida
        confirmable = update.get("update_id", confirmable)

    _confirmar(bot, confirmable, result, dry_run)
    return result


def _anunciar(
    answer: dict,
    store: Store,
    bot: Bot,
    nombres: dict[str, str],
    now: datetime,
    result: RespuestasResult,
    dry_run: bool,
) -> bool:
    """Avisa un voto al chat. False solo si el aviso no pudo salir."""
    poll_id = str(answer.get("poll_id") or "")
    user = answer.get("user") or {}
    user_id = str(user.get("id") or "")
    marcadas = [int(i) for i in answer.get("option_ids") or []]

    if not poll_id or not user_id:
        # Un voto de un canal anónimo llega con `voter_chat` en vez de `user`.
        # No hay a quién nombrar, así que tampoco hay nada útil que contar.
        result.lineas.append("voto sin usuario identificable; se ignora")
        return True

    info = store.poll_info(poll_id)
    if info is None:
        result.desconocidas += 1
        result.lineas.append("voto de una encuesta sin registrar (vieja o ya limpiada)")
        return True

    if dry_run:
        result.avisos += 1
        result.lineas.append(f"avisaría {info.reminder_id} — {_marca(marcadas)}")
        return True

    if not store.register_answer(poll_id, user_id, marcadas, now):
        result.repetidas += 1
        result.lineas.append(f"repetida {info.reminder_id} — ya se había avisado")
        return True

    texto = _texto(nombres.get(info.reminder_id, info.reminder_id), user, marcadas, info.options)
    try:
        bot.send_message(
            chat_id=info.chat_id,
            text=texto,
            parse_mode="HTML",
            reply_to_message_id=info.message_id,
        )
    except Exception as exc:
        # Se deshace el registro: si quedara marcada como avisada, el reintento
        # la saltaría y la novedad se perdería en silencio.
        store.forget_answer(poll_id, user_id)
        result.error = f"{type(exc).__name__}: {exc}"
        result.lineas.append(f"sin avisar {info.reminder_id} — {result.error}")
        return False

    result.avisos += 1
    result.lineas.append(f"avisado {info.reminder_id} — {_marca(marcadas)}")
    return True


def _confirmar(bot: Bot, ultimo: int | None, result: RespuestasResult, dry_run: bool) -> None:
    """Le dice a Telegram hasta dónde leímos. Lo anterior se borra y no vuelve.

    Se confirma hasta el último voto efectivamente resuelto, nunca más allá: lo
    que quedó sin avisar tiene que volver a llegar. La llamada puede traer de
    vuelta actualizaciones más nuevas, y está bien — esas no se confirman, así
    que siguen esperando a la próxima corrida.
    """
    if ultimo is None or dry_run:
        return
    try:
        bot.get_updates(offset=int(ultimo) + 1, limit=1)
    except Exception as exc:
        # Sin confirmar, la corrida siguiente vuelve a ver lo mismo. No se
        # duplica nada: `register_answer` ya sabe qué se avisó.
        result.error = result.error or f"sin confirmar la lectura ({type(exc).__name__}: {exc})"
        return
    result.confirmado = int(ultimo)


def _texto(recordatorio: str, user: dict, marcadas: list[int], opciones: tuple[str, ...]) -> str:
    """El mensaje que ve el grupo.

    Dice quién, en qué recordatorio y qué marcó. El texto de la opción ya
    explica qué hace falta ("¿quién está en la casa para que me haga el
    favor?"), así que no hace falta agregarle nada.
    """
    quien = html.escape(_nombre(user))
    donde = html.escape(recordatorio)

    if not marcadas:
        return f"↩️ <b>{quien}</b> retiró su respuesta en «{donde}»."

    elegidas = [opciones[i] for i in marcadas if 0 <= i < len(opciones)]
    if not elegidas:
        # La opción no está en la lista que guardamos: pasa si alguien editó el
        # YAML entre el envío y el voto. Mejor un aviso incompleto que ninguno.
        return f"📣 <b>{quien}</b> respondió en «{donde}»."

    detalle = "\n".join(html.escape(opcion) for opcion in elegidas)
    return f"📣 <b>{quien}</b> respondió en «{donde}»:\n{detalle}"


def _nombre(user: dict) -> str:
    """Cómo llamar a quien votó. El nombre de pila alcanza en un grupo de casa."""
    for campo in ("first_name", "username", "last_name"):
        valor = str(user.get(campo) or "").strip()
        if valor:
            return valor
    return "Alguien"


def _marca(marcadas: list[int]) -> str:
    """Qué se marcó, para el log. Va sin nombres: este log es público."""
    if not marcadas:
        return "retiró el voto"
    return "opción " + ", ".join(str(i + 1) for i in marcadas)
