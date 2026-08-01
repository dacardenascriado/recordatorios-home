"""La idempotencia vive en el store: un claim por ocurrencia."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recordatorios.store import RETENTION_DAYS, STALE_CLAIM_MINUTES, Store

UTC = timezone.utc
AHORA = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
OCURRENCIA = datetime(2026, 8, 3, 11, 58, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = Store.open(None, sqlite_path=tmp_path / "test.db")
    s.init_schema()
    yield s
    s.close()


def test_no_conecta_hasta_la_primera_consulta(tmp_path):
    s = Store.open(None, sqlite_path=tmp_path / "perezoso.db")
    assert s.connected is False

    s.init_schema()
    assert s.connected is True

    s.close()
    assert s.connected is False


def test_solo_el_primer_claim_gana(store):
    assert store.claim("r", OCURRENCIA, AHORA) is True
    assert store.claim("r", OCURRENCIA, AHORA) is False


def test_una_vez_enviado_no_se_vuelve_a_reclamar(store):
    store.claim("r", OCURRENCIA, AHORA)
    store.mark_sent("r", OCURRENCIA, AHORA)

    assert store.claim("r", OCURRENCIA, AHORA + timedelta(days=1)) is False


def test_un_fallo_se_puede_reclamar_de_inmediato(store):
    store.claim("r", OCURRENCIA, AHORA)
    store.mark_failed("r", OCURRENCIA, "timeout", AHORA)

    assert store.claim("r", OCURRENCIA, AHORA + timedelta(seconds=30)) is True


def test_un_claim_abandonado_se_puede_retomar(store):
    # El runner murió a mitad de envío: la fila quedó en 'sending'.
    store.claim("r", OCURRENCIA, AHORA)

    apenas_despues = AHORA + timedelta(minutes=STALE_CLAIM_MINUTES - 1)
    assert store.claim("r", OCURRENCIA, apenas_despues) is False

    ya_vencido = AHORA + timedelta(minutes=STALE_CLAIM_MINUTES + 1)
    assert store.claim("r", OCURRENCIA, ya_vencido) is True


def test_ocurrencias_distintas_del_mismo_recordatorio_son_independientes(store):
    otra = OCURRENCIA + timedelta(days=14)
    assert store.claim("r", OCURRENCIA, AHORA) is True
    assert store.claim("r", otra, AHORA) is True


def test_el_historial_conserva_cada_intento(store):
    store.claim("r", OCURRENCIA, AHORA)
    store.mark_failed("r", OCURRENCIA, "timeout", AHORA)
    store.claim("r", OCURRENCIA, AHORA + timedelta(minutes=1))
    store.mark_sent("r", OCURRENCIA, AHORA + timedelta(minutes=1))

    assert [fila.status for fila in store.history()] == ["sent", "failed"]  # reciente primero


def test_prune_borra_lo_viejo_y_respeta_lo_reciente(store):
    vieja = AHORA - timedelta(days=RETENTION_DAYS + 5)
    store.claim("r", vieja, vieja)
    store.mark_sent("r", vieja, vieja)
    store.claim("r", OCURRENCIA, AHORA)
    store.mark_sent("r", OCURRENCIA, AHORA)

    store.prune(AHORA)

    assert [fila.occurrence_at for fila in store.history()] == [OCURRENCIA]
    # Y lo borrado se puede volver a reclamar, porque ya no hay rastro.
    assert store.claim("r", vieja, AHORA) is True


def test_init_schema_es_repetible(store):
    store.init_schema()
    store.init_schema()
    assert store.history() == []
