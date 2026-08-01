"""Que el token no se escape a un log.

Va en la URL de cada llamada, y los logs de GitHub Actions son públicos en un
repo público. GitHub enmascara los secrets que reconoce, pero acá no queremos
depender de eso.
"""

from __future__ import annotations

from recordatorios.telegram import redact

TOKEN = "8870339268:AAF6S7FlAqbcrzFG-mQZSBY0mtELX0uyPk8"


def test_borra_el_token_de_una_url():
    crudo = f"error de red: ConnectTimeout en https://api.telegram.org/bot{TOKEN}/sendMessage"

    limpio = redact(crudo, TOKEN)

    assert TOKEN not in limpio
    assert "https://api.telegram.org/bot***/sendMessage" in limpio


def test_borra_todas_las_apariciones():
    crudo = f"{TOKEN} falló, reintentando con {TOKEN}"
    assert redact(crudo, TOKEN).count("***") == 2


def test_deja_intacto_lo_que_no_es_el_token():
    crudo = "HTTP 400: chat not found"
    assert redact(crudo, TOKEN) == crudo


def test_sin_token_no_rompe():
    assert redact("algo", None) == "algo"
    assert redact("algo", "") == "algo"
