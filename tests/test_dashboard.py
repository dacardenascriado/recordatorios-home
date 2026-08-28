"""El dashboard: que diga la verdad sobre lo que salió, y que no publique nombres.

Las dos cosas importan por separado. La primera porque la página existe para
detectar el fallo que en agosto de 2026 nadie vio: una ocurrencia que ningún
tick llegó a mirar. La segunda porque el sitio es público y los nombres viven en
secrets justamente para no estarlo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recordatorios.dashboard import ENVIADO, EN_CURSO, PERDIDO, VENCIDO, construir
from recordatorios.render import render
from recordatorios.models import Reminder
from recordatorios.store import Store

UTC = timezone.utc

# Lunes 2026-08-03, 07:00 en Bogotá.
LUNES = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(None, sqlite_path=tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def semanal(**overrides) -> Reminder:
    base = dict(
        id="basura",
        name="Basura",
        cron="0 7 * * 1",
        messages=("sacar la basura",),
        chat_id="555",
        timezone="America/Bogota",
        max_delay_minutes=120,
    )
    base.update(overrides)
    return Reminder(**base)


def test_una_ocurrencia_enviada_se_ve_como_enviada(store):
    store.claim("basura", LUNES, LUNES)
    store.mark_sent("basura", LUNES, LUNES + timedelta(minutes=2))

    resumen = construir([semanal()], store, now=LUNES + timedelta(days=1))

    assert [f.estado for f in resumen.pasado] == [ENVIADO]
    assert resumen.problemas == []
    assert resumen.salud == "al día"


def test_una_ocurrencia_sin_fila_y_ya_vencida_es_una_perdida(store):
    # Es el caso que nadie veía: ningún tick la miró, así que no hay ni fila de
    # 'stale'. Solo se detecta cruzando el calendario contra la base.
    resumen = construir([semanal()], store, now=LUNES + timedelta(days=1))

    assert [f.estado for f in resumen.pasado] == [PERDIDO]
    assert resumen.salud == "con problemas"
    assert "ningún tick la vio" in resumen.pasado[0].detalle


def test_una_ocurrencia_recien_pasada_no_es_una_perdida(store):
    # Todavía dentro de max_delay: el tick puede estar por llegar. Marcarla como
    # perdida acá sería una falsa alarma, y una alarma que miente se ignora.
    resumen = construir([semanal()], store, now=LUNES + timedelta(minutes=30))

    assert [f.estado for f in resumen.pasado] == [EN_CURSO]
    assert resumen.problemas == []


def test_lo_descartado_por_viejo_se_ve_como_vencido(store):
    store.mark_stale("basura", LUNES, "muy tarde", LUNES + timedelta(hours=3))

    resumen = construir([semanal()], store, now=LUNES + timedelta(days=1))

    assert [f.estado for f in resumen.pasado] == [VENCIDO]
    assert resumen.problemas != []


def test_los_pausados_no_cuentan(store):
    resumen = construir([semanal(enabled=False)], store, now=LUNES + timedelta(days=1))

    assert resumen.pasado == []
    assert resumen.futuro == []


def test_la_pagina_no_contiene_nombres(store):
    # La rotación lleva nombres y el chat_id es un identificador; ninguno de los
    # dos puede terminar en una URL pública.
    reminder = semanal(
        rotation=("Ana Restrepo", "Beto Gómez"),
        messages=("le toca a {turno}",),
        anchor=LUNES.date(),
        chat_id="-1001234567890",
    )
    store.claim("basura", LUNES, LUNES)
    store.mark_sent("basura", LUNES, LUNES + timedelta(minutes=2))

    html = render(construir([reminder], store, now=LUNES + timedelta(days=1)))

    assert "Ana Restrepo" not in html
    assert "Beto Gómez" not in html
    assert "-1001234567890" not in html
    assert "le toca a" not in html
    # Lo que sí tiene que estar: el id, para saber de qué recordatorio se habla.
    assert "basura" in html


def test_la_pagina_no_carga_recursos_externos(store):
    # Se sirve desde Pages sin backend; cualquier recurso externo sería una
    # dependencia que se puede caer y un rastreador que nadie pidió. El único
    # enlace saliente permitido es el que lleva a Actions a reenviar algo, y
    # ese aparece solo si se pasó --repo.
    html = render(construir([semanal()], store, now=LUNES + timedelta(days=1)))

    assert "<script src" not in html
    assert "<link" not in html
    assert 'src="http' not in html
    assert 'href="http' not in html


def test_el_unico_enlace_externo_es_el_de_reenviar(store):
    reminder = semanal(max_delay_minutes=30)
    resumen = construir([reminder], store, now=LUNES + timedelta(days=1))
    assert resumen.problemas, "el caso de prueba necesita un problema"

    html = render(resumen, repo="usuario/repo")

    assert "https://github.com/usuario/repo/actions/workflows/tick.yml" in html
    assert 'rel="noopener"' in html
    # El id va en el botón de copiar: GitHub no admite prellenar el input.
    assert 'data-copiar="basura"' in html


def test_sin_repo_no_hay_botones(store):
    # En local no hay a dónde enlazar, y un botón que no lleva a ningún lado es
    # peor que ninguno.
    reminder = semanal(max_delay_minutes=30)

    html = render(construir([reminder], store, now=LUNES + timedelta(days=1)))

    assert "Mandarlo ahora" not in html
    assert "data-copiar" not in html
    # El id sigue estando, para poder copiarlo a mano.
    assert "basura" in html


def test_el_futuro_no_se_pasa_del_horizonte(store):
    resumen = construir([semanal()], store, now=LUNES, dias_adelante=14)

    assert resumen.futuro
    assert all(cuando <= LUNES + timedelta(days=14) for _, cuando in resumen.futuro)
