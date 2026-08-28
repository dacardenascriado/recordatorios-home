"""Registro de qué ocurrencias ya se enviaron.

Los runners de GitHub Actions son efímeros, así que este estado tiene que vivir
fuera del repo. En producción es Neon (Postgres); en local/tests, SQLite.

La idempotencia sale de la clave primaria (reminder_id, occurrence_at): dos
ticks solapados intentan insertar la misma fila y solo uno gana el "claim". No
hace falta ningún cursor de "hasta dónde llegué": cada ocurrencia se identifica
por sí misma.

La conexión se abre de forma perezosa, en la primera consulta. Un tick sin nada
que enviar nunca la abre, y eso es lo que mantiene el compute de Neon dormido
casi todo el mes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Un claim en estado 'sending' más viejo que esto se considera abandonado
# (el runner murió a mitad de envío) y otro tick puede retomarlo.
STALE_CLAIM_MINUTES = 10

# Las entregas viejas no le sirven a nadie y el plan gratuito de Neon trae
# 0.5 GB. Se limpian solas.
RETENTION_DAYS = 90

SENT = "sent"
SENDING = "sending"
FAILED = "failed"
# Vencido: se pasó de max_delay_minutes y ya no se va a enviar. Es terminal,
# igual que 'sent': el claim no lo retoma.
STALE = "stale"
# Descartado a mano: alguien vio la pérdida y decidió que ya no importa.
# No borra nada —la ocurrencia sigue en el historial— pero deja de contar
# como problema pendiente.
DISMISSED = "dismissed"


@dataclass(frozen=True)
class DeliveryRow:
    reminder_id: str
    occurrence_at: datetime
    status: str
    detail: str | None
    logged_at: datetime


class Store:
    """Persistencia sobre Postgres o SQLite con el mismo SQL."""

    def __init__(self, dialect: _Dialect) -> None:
        self._dialect = dialect

    # -- ciclo de vida ---------------------------------------------------

    @classmethod
    def open(cls, database_url: str | None, sqlite_path: str | Path = "recordatorios.db") -> Store:
        """Elige backend según haya DATABASE_URL o no. No conecta todavía."""
        if database_url:
            return cls(_PostgresDialect(database_url))
        return cls(_SqliteDialect(str(sqlite_path)))

    @property
    def backend(self) -> str:
        return self._dialect.name

    @property
    def connected(self) -> bool:
        return self._dialect.connected

    def close(self) -> None:
        self._dialect.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- esquema ---------------------------------------------------------

    def init_schema(self) -> None:
        ts = self._dialect.timestamp_type
        with self._dialect.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS deliveries (
                    reminder_id   TEXT NOT NULL,
                    occurrence_at {ts} NOT NULL,
                    status        TEXT NOT NULL,
                    attempts      INTEGER NOT NULL DEFAULT 1,
                    claimed_at    {ts} NOT NULL,
                    sent_at       {ts},
                    detail        TEXT,
                    PRIMARY KEY (reminder_id, occurrence_at)
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS delivery_log (
                    reminder_id   TEXT NOT NULL,
                    occurrence_at {ts} NOT NULL,
                    status        TEXT NOT NULL,
                    detail        TEXT,
                    logged_at     {ts} NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS delivery_log_logged_at "
                "ON delivery_log (logged_at DESC)"
            )

    def prune(self, now: datetime, retention_days: int = RETENTION_DAYS) -> None:
        """Borra el historial viejo. Barato: solo corre cuando hay algo que enviar."""
        corte = self._dialect.encode_ts(now - timedelta(days=retention_days))
        with self._dialect.cursor() as cur:
            cur.execute(self._sql("DELETE FROM deliveries WHERE occurrence_at < {p}"), (corte,))
            cur.execute(self._sql("DELETE FROM delivery_log WHERE logged_at < {p}"), (corte,))

    # -- entregas ---------------------------------------------------------

    def claim(self, reminder_id: str, occurrence_at: datetime, now: datetime) -> bool:
        """Reserva el envío de una ocurrencia. True si nos toca a nosotros.

        Devuelve False si ya se envió o si otra corrida la tiene reservada y su
        reserva sigue vigente. Un intento fallido anterior sí se puede
        reintentar de inmediato.
        """
        stale_before = now - timedelta(minutes=STALE_CLAIM_MINUTES)
        params = (
            reminder_id,
            self._dialect.encode_ts(occurrence_at),
            SENDING,
            self._dialect.encode_ts(now),
            FAILED,
            SENDING,
            self._dialect.encode_ts(stale_before),
        )
        # Solo se re-reclama lo que sigue abierto: un intento fallido (se
        # reintenta ya) o un envío que quedó colgado. Lo que está en 'sent' es
        # definitivo.
        sql = self._sql(
            """
            INSERT INTO deliveries (reminder_id, occurrence_at, status, attempts, claimed_at)
            VALUES ({p}, {p}, {p}, 1, {p})
            ON CONFLICT (reminder_id, occurrence_at) DO UPDATE
                SET status     = {excluded}.status,
                    claimed_at = {excluded}.claimed_at,
                    attempts   = deliveries.attempts + 1
                WHERE deliveries.status = {p}
                   OR (deliveries.status = {p} AND deliveries.claimed_at < {p})
            RETURNING 1
            """
        )
        with self._dialect.cursor() as cur:
            # RETURNING solo devuelve fila si el INSERT o el UPDATE ocurrió de
            # verdad; si el WHERE del upsert no se cumple, no hay claim.
            # (Postgres 9.5+ y SQLite 3.35+.)
            cur.execute(sql, params)
            return cur.fetchone() is not None

    def mark_stale(
        self, reminder_id: str, occurrence_at: datetime, detail: str, now: datetime
    ) -> bool:
        """Anota que una ocurrencia se descartó por vieja. True si es la primera vez.

        Una ocurrencia vencida sigue apareciendo en la ventana un buen rato
        después de vencerse, así que la ven muchos ticks seguidos. Esto la
        registra una sola vez: la clave primaria hace de memoria, igual que en
        `claim`. Solo pisa un intento fallido —ese es el que terminó de
        vencerse— y nunca un 'sent'.
        """
        params = (
            reminder_id,
            self._dialect.encode_ts(occurrence_at),
            STALE,
            self._dialect.encode_ts(now),
            detail,
            FAILED,
        )
        sql = self._sql(
            """
            INSERT INTO deliveries
                (reminder_id, occurrence_at, status, attempts, claimed_at, detail)
            VALUES ({p}, {p}, {p}, 1, {p}, {p})
            ON CONFLICT (reminder_id, occurrence_at) DO UPDATE
                SET status = {excluded}.status,
                    detail = {excluded}.detail
                WHERE deliveries.status = {p}
            RETURNING 1
            """
        )
        with self._dialect.cursor() as cur:
            cur.execute(sql, params)
            if cur.fetchone() is None:
                return False
        self.log(reminder_id, occurrence_at, STALE, detail, now)
        return True

    def dismiss(self, reminder_id: str, occurrence_at: datetime, now: datetime) -> bool:
        """Marca una ocurrencia como vista y superada. True si cambió algo.

        Reenviar el aviso del baño de anteayer no le sirve a nadie, pero dejarlo
        para siempre en la lista de problemas tampoco: la alarma que nunca se
        apaga se deja de mirar. Descartar es la tercera opción — la pérdida
        queda en el historial, pero deja de pedir acción.

        Una pérdida sin fila en la base (nadie la vio) se inserta ya descartada;
        una vencida o fallida se pisa. Un 'sent' no se toca nunca: descartar es
        para lo que no salió, y falsear un envío sería peor que el problema.
        """
        params = (
            reminder_id,
            self._dialect.encode_ts(occurrence_at),
            DISMISSED,
            self._dialect.encode_ts(now),
            "descartado a mano",
            SENT,
        )
        sql = self._sql(
            """
            INSERT INTO deliveries
                (reminder_id, occurrence_at, status, attempts, claimed_at, detail)
            VALUES ({p}, {p}, {p}, 0, {p}, {p})
            ON CONFLICT (reminder_id, occurrence_at) DO UPDATE
                SET status = {excluded}.status,
                    detail = {excluded}.detail
                WHERE deliveries.status <> {p}
            RETURNING 1
            """
        )
        with self._dialect.cursor() as cur:
            cur.execute(sql, params)
            if cur.fetchone() is None:
                return False
        self.log(reminder_id, occurrence_at, DISMISSED, "descartado a mano", now)
        return True

    def mark_sent(self, reminder_id: str, occurrence_at: datetime, now: datetime) -> None:
        self._finish(reminder_id, occurrence_at, SENT, None, now)

    def mark_failed(
        self, reminder_id: str, occurrence_at: datetime, detail: str, now: datetime
    ) -> None:
        self._finish(reminder_id, occurrence_at, FAILED, detail, now)

    def _finish(
        self,
        reminder_id: str,
        occurrence_at: datetime,
        status: str,
        detail: str | None,
        now: datetime,
    ) -> None:
        sent_at = self._dialect.encode_ts(now) if status == SENT else None
        with self._dialect.cursor() as cur:
            cur.execute(
                self._sql(
                    """
                    UPDATE deliveries SET status = {p}, detail = {p}, sent_at = {p}
                    WHERE reminder_id = {p} AND occurrence_at = {p}
                    """
                ),
                (
                    status,
                    detail,
                    sent_at,
                    reminder_id,
                    self._dialect.encode_ts(occurrence_at),
                ),
            )
        self.log(reminder_id, occurrence_at, status, detail, now)

    def log(
        self,
        reminder_id: str,
        occurrence_at: datetime,
        status: str,
        detail: str | None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        with self._dialect.cursor() as cur:
            cur.execute(
                self._sql(
                    """
                    INSERT INTO delivery_log (reminder_id, occurrence_at, status, detail, logged_at)
                    VALUES ({p}, {p}, {p}, {p}, {p})
                    """
                ),
                (
                    reminder_id,
                    self._dialect.encode_ts(occurrence_at),
                    status,
                    detail,
                    self._dialect.encode_ts(now),
                ),
            )

    def history(self, limit: int = 20) -> list[DeliveryRow]:
        rows = self._query(
            self._sql(
                """
                SELECT reminder_id, occurrence_at, status, detail, logged_at
                FROM delivery_log ORDER BY logged_at DESC LIMIT {p}
                """
            ),
            (limit,),
        )
        return [
            DeliveryRow(
                reminder_id=row[0],
                occurrence_at=self._dialect.decode_ts(row[1]),
                status=row[2],
                detail=row[3],
                logged_at=self._dialect.decode_ts(row[4]),
            )
            for row in rows
        ]

    def deliveries_between(self, start: datetime, end: datetime) -> list[DeliveryRow]:
        """Estado actual de cada ocurrencia en la ventana [start, end].

        Lee `deliveries` y no `delivery_log` a propósito: el log tiene una fila
        por intento (un 'failed' seguido de un 'sent' son dos), y acá se quiere
        una respuesta por ocurrencia — en qué terminó.

        `logged_at` trae el `sent_at` cuando lo hay, y si no el `claimed_at`:
        el dashboard lo usa para decir a qué hora salió de verdad.
        """
        rows = self._query(
            self._sql(
                """
                SELECT reminder_id, occurrence_at, status, detail,
                       COALESCE(sent_at, claimed_at)
                FROM deliveries
                WHERE occurrence_at >= {p} AND occurrence_at <= {p}
                ORDER BY occurrence_at
                """
            ),
            (self._dialect.encode_ts(start), self._dialect.encode_ts(end)),
        )
        return [
            DeliveryRow(
                reminder_id=row[0],
                occurrence_at=self._dialect.decode_ts(row[1]),
                status=row[2],
                detail=row[3],
                logged_at=self._dialect.decode_ts(row[4]),
            )
            for row in rows
        ]

    # -- utilidades -------------------------------------------------------

    def _sql(self, template: str) -> str:
        return template.format(p=self._dialect.placeholder, excluded=self._dialect.excluded_alias)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._dialect.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


class _Dialect:
    """Conexión perezosa: se abre en la primera consulta, no antes."""

    name = "?"
    placeholder = "?"
    excluded_alias = "excluded"
    timestamp_type = "TEXT"

    def __init__(self) -> None:
        self._conn: Any = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _connect(self) -> Any:
        raise NotImplementedError

    def _handle(self) -> Any:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        conn = self._handle()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def encode_ts(self, moment: datetime) -> Any:
        raise NotImplementedError

    def decode_ts(self, value: Any) -> datetime:
        raise NotImplementedError


class _SqliteDialect(_Dialect):
    """SQLite guarda los instantes como epoch en segundos (float, UTC)."""

    name = "sqlite"
    placeholder = "?"
    excluded_alias = "excluded"
    timestamp_type = "REAL"

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def _connect(self) -> Any:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def encode_ts(self, moment: datetime) -> float:
        return _as_utc(moment).timestamp()

    def decode_ts(self, value: Any) -> datetime:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)


class _PostgresDialect(_Dialect):
    """Neon/Postgres con psycopg 3 y timestamptz nativo."""

    name = "postgres"
    placeholder = "%s"
    excluded_alias = "EXCLUDED"
    timestamp_type = "TIMESTAMPTZ"

    def __init__(self, dsn: str) -> None:
        super().__init__()
        self._dsn = dsn

    def _connect(self) -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "Hay DATABASE_URL pero falta psycopg. Instala: pip install '.[postgres]'"
            ) from exc
        conn = psycopg.connect(self._dsn, autocommit=False)
        # El pooler de Neon es PgBouncer en modo transacción y no soporta
        # prepared statements. Desactivarlos hace que funcionen por igual la
        # cadena "pooled" y la directa, sin tener que elegir bien.
        conn.prepare_threshold = None
        return conn

    def encode_ts(self, moment: datetime) -> datetime:
        return _as_utc(moment)

    def decode_ts(self, value: Any) -> datetime:
        if isinstance(value, datetime):
            return _as_utc(value)
        return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
