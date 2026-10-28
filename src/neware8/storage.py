"""Transactional current-run recording and bounded-memory BDF Parquet export."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
import logging
import json
import shutil
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
from uuid import UUID, uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from .measurements import current_scale
from .messages import HistoryBatch, HistoryRecord
from . import _bdf
from ._counters import CounterTotals
from .errors import StateError, StorageError
from .models import (
    ChannelAddress,
    ChannelProfile,
    DataCompleteness,
    Export,
    RecordedPoint,
    RecordChange,
    Run,
    RunState,
    bounded_integer,
)


logger = logging.getLogger(__name__)


class MeasurementStore:
    """One controller's latest run per channel, including its managed export files."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._export_lock = threading.RLock()
        self._db = sqlite3.connect(self.directory / "measurements.sqlite", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 2):
            self._db.close()
            raise StorageError(f"unsupported storage schema {version}; expected 2")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, unit INTEGER NOT NULL, channel INTEGER NOT NULL,
                test_id INTEGER NOT NULL, state TEXT NOT NULL, physical_id INTEGER NOT NULL,
                started_ns INTEGER NOT NULL, xml BLOB, charge_range INTEGER,
                UNIQUE(unit, channel)
            );
            CREATE TABLE IF NOT EXISTS records (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL, raw BLOB NOT NULL,
                PRIMARY KEY(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS opaque_records (
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                raw BLOB NOT NULL, PRIMARY KEY(run_id, raw)
            );
            CREATE TABLE IF NOT EXISTS exports (
                path TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                sha256 TEXT, previous_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_deletions (
                path TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                sha256 TEXT, previous_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS archives (
                run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                path TEXT NOT NULL, record_count INTEGER NOT NULL, gaps TEXT NOT NULL
            );
            PRAGMA user_version=2;
        """)
        self._closed = False
        try:
            self._purge_exports()
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StorageError("measurement store is closed")
            try:
                with self._db:
                    yield self._db
            except sqlite3.Error as error:
                logger.exception("Storage transaction failed directory=%s", self.directory)
                raise StorageError(str(error)) from error

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StorageError("measurement store is closed")
            db = sqlite3.connect(
                (self.directory / "measurements.sqlite").as_uri() + "?mode=ro", uri=True
            )
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
        try:
            yield db
        finally:
            db.close()

    def bind(self, identity: bytes) -> None:
        """Bind only to the opaque identity from a fresh native 002A response."""
        if len(identity) != 16:
            raise ValueError("controller identity must contain 16 bytes")
        with self._transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='controller'").fetchone()
            if row is not None and row[0] != identity.hex():
                raise StorageError("storage belongs to a different controller")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('controller', ?)", (identity.hex(),))

    def begin(
        self, profile: ChannelProfile, test_id: int, xml: bytes | None, run_id: str | None = None
    ) -> Run:
        """Replace a channel's working data after a new TestID is assigned."""
        bounded_integer("test_id", test_id, 2**32 - 1)
        address = profile.address
        run_id = uuid4().hex if run_id is None else UUID(run_id).hex
        with self._export_lock, self._transaction() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='controller'").fetchone() is None:
                raise StateError("bind the controller before recording")
            previous = db.execute(
                "SELECT test_id, id FROM runs WHERE unit=? AND channel=?",
                (address.unit, address.channel),
            ).fetchone()
            if previous is not None and previous[0] == test_id:
                raise StateError("TestID is already recorded; reconcile it instead of replacing it")
            if previous is not None:
                db.execute(
                    "INSERT OR REPLACE INTO pending_deletions SELECT * FROM exports WHERE run_id=?",
                    (previous[1],),
                )
                db.execute("DELETE FROM exports WHERE run_id=?", (previous[1],))
            db.execute(
                "DELETE FROM runs WHERE unit=? AND channel=?", (address.unit, address.channel)
            )
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    address.unit,
                    address.channel,
                    test_id,
                    RunState.RECORDING.value,
                    profile.physical_id,
                    time.time_ns(),
                    xml,
                    profile.charge_range_raw,
                ),
            )
        self._purge_exports()
        logger.info(
            "Recording ownership assigned channel=%s run_id=%s test_id=%d", address, run_id, test_id
        )
        return self.get(run_id)

    def clear(self, address: ChannelAddress) -> None:
        """Delete a channel's current records and managed exports without device writes."""
        logger.info("Clearing retained data channel=%s", address)
        with self._export_lock, self._transaction() as db:
            row = db.execute(
                "SELECT id FROM runs WHERE unit=? AND channel=?", (address.unit, address.channel)
            ).fetchone()
            if row is not None:
                db.execute(
                    "INSERT OR REPLACE INTO pending_deletions SELECT * FROM exports WHERE run_id=?",
                    (row[0],),
                )
                db.execute("DELETE FROM exports WHERE run_id=?", (row[0],))
                db.execute("DELETE FROM runs WHERE id=?", (row[0],))
        self._purge_exports()

    def _purge_exports(self) -> None:
        with self._export_lock, self._transaction() as db:
            rows = db.execute("SELECT * FROM pending_deletions").fetchall()
            for row in rows:
                path = Path(row["path"])
                if path.exists():
                    if self._file_hash(path) not in (row["sha256"], row["previous_sha256"]):
                        raise StorageError(f"retained export was replaced by another file: {path}")
                    path.unlink()
                db.execute("DELETE FROM pending_deletions WHERE path=?", (str(path),))

    @staticmethod
    def _file_hash(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _run(self, row: sqlite3.Row, count: int, gaps: bool = False) -> Run:
        return Run(
            row["id"],
            ChannelAddress(row["unit"], row["channel"]),
            row["test_id"],
            RunState(row["state"]),
            row["physical_id"],
            row["started_ns"],
            count,
            DataCompleteness.GAPS if gaps else DataCompleteness.UNKNOWN,
        )

    def get(self, run_id: str) -> Run:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            archive = db.execute("SELECT * FROM archives WHERE run_id=?", (run_id,)).fetchone()
            if archive is not None:
                return self._run(row, archive["record_count"], bool(json.loads(archive["gaps"])))
            count, low, high = db.execute(
                "SELECT count(*), min(sequence), max(sequence) FROM records WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return self._run(row, count, bool(count and (low != 1 or high != count)))

    def charge_range(self, run_id: str) -> int | None:
        with self._transaction() as db:
            row = db.execute("SELECT charge_range FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return None if row[0] is None else int(row[0])

    def runs(self) -> tuple[Run, ...]:
        with self._transaction() as db:
            rows = db.execute("SELECT id FROM runs ORDER BY unit, channel").fetchall()
            return tuple(self.get(row[0]) for row in rows)

    def set_state(self, run_id: str, state: RunState) -> None:
        with self._transaction() as db:
            result = db.execute("UPDATE runs SET state=? WHERE id=?", (state.value, run_id))
            if result.rowcount != 1:
                raise KeyError(run_id)

    def append(self, batch: HistoryBatch, eligible_runs: Sequence[str]) -> tuple[RecordChange, ...]:
        """Commit selected records before the caller acknowledges the controller batch."""
        eligible = set(eligible_runs)
        changed: dict[str, RecordChange] = {}
        highwater: dict[str, int] = {}
        with self._transaction() as db:
            rows = db.execute("SELECT id, unit, channel, test_id FROM runs").fetchall()
            selected = {
                (r["unit"], r["channel"], r["test_id"]): r["id"]
                for r in rows
                if r["id"] in eligible
            }
            for item in batch.records:
                run_id = selected.get((item.unit, item.channel, item.test_id))
                if run_id is None:
                    continue
                if db.execute("SELECT 1 FROM archives WHERE run_id=?", (run_id,)).fetchone():
                    raise StorageError(f"history arrived after finalization for run {run_id}")
                raw = item.raw
                if item.data_flag != HistoryRecord.MAIN or item.aux:
                    db.execute("INSERT OR IGNORE INTO opaque_records VALUES (?, ?)", (run_id, raw))
                    continue
                sequence = item.sequence
                previous = db.execute(
                    "SELECT raw FROM records WHERE run_id=? AND sequence=?", (run_id, sequence)
                ).fetchone()
                if previous is not None:
                    if previous[0] != raw:
                        logger.error(
                            "Conflicting history; rolling back batch run_id=%s sequence=%d",
                            run_id,
                            sequence,
                        )
                        raise StorageError(f"conflicting history at sequence {sequence}")
                    continue
                if run_id not in highwater:
                    highwater[run_id] = db.execute(
                        "SELECT coalesce(max(sequence), 0) FROM records WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                db.execute("INSERT INTO records VALUES (?, ?, ?)", (run_id, sequence, raw))
                prior = changed.get(run_id)
                changed[run_id] = RecordChange(
                    run_id,
                    min(prior.first_sequence, sequence) if prior else sequence,
                    max(prior.last_sequence, sequence) if prior else sequence,
                    (prior.backfill if prior else False) or sequence <= highwater[run_id],
                )
        for change in changed.values():
            logger.debug(
                "History committed run_id=%s first_sequence=%d last_sequence=%d backfill=%s",
                change.run_id,
                change.first_sequence,
                change.last_sequence,
                change.backfill,
            )
        return tuple(changed[key] for key in sorted(changed))

    def _point(self, raw: bytes, charge_range: int | None) -> tuple[RecordedPoint, HistoryRecord]:
        record = HistoryRecord.from_bytes(raw)
        range_raw = record.range_raw or charge_range
        current = None
        if range_raw is not None:
            try:
                current = record.current_raw / (current_scale(range_raw) * 1000)
            except ValueError:
                pass
        return RecordedPoint(
            record.sequence,
            record.total_time_ms / 1000,
            record.voltage_raw / 10000,
            current,
            record.cycle,
            record.step_id,
            raw,
        ), record

    def read(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 4096
    ) -> tuple[RecordedPoint, ...]:
        bounded_integer("after_sequence", after_sequence, 2**32 - 1)
        bounded_integer("limit", limit, 100_000, 1)
        with self._transaction() as db:
            row = db.execute("SELECT charge_range FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            archive = db.execute("SELECT path FROM archives WHERE run_id=?", (run_id,)).fetchone()
            if archive is not None:
                return self._archived_points(archive[0], after_sequence, limit)
            points = []
            try:
                for point, _ in self._points(db, run_id, row[0], after_sequence, limit):
                    points.append(point)
            except _bdf.PendingRecordTime:
                pass
            return tuple(points)

    @staticmethod
    def _archived_points(path: str, after_sequence: int, limit: int) -> tuple[RecordedPoint, ...]:
        points = []
        columns = [
            "record_index",
            "test_time_second",
            "voltage_volt",
            "current_ampere",
            "cycle_count",
            "step_id",
        ]
        with pq.ParquetFile(path) as parquet:
            index = parquet.schema_arrow.get_field_index("record_index")
            groups = []
            for group in range(parquet.num_row_groups):
                metadata = parquet.metadata.row_group(group)
                if not metadata.num_rows:
                    continue
                statistics = metadata.column(index).statistics
                if statistics is None or not statistics.has_min_max:
                    raise StorageError("finalized archive is missing record index statistics")
                if statistics.max > after_sequence:
                    groups.append(group)
            for batch in parquet.iter_batches(batch_size=4096, columns=columns, row_groups=groups):
                for item in batch.to_pylist():
                    if item["record_index"] <= after_sequence:
                        continue
                    points.append(
                        RecordedPoint(
                            item["record_index"],
                            item["test_time_second"],
                            item["voltage_volt"],
                            item["current_ampere"],
                            item["cycle_count"],
                            item["step_id"],
                            b"",
                        )
                    )
                    if len(points) == limit:
                        return tuple(points)
        return tuple(points)

    def _points(
        self,
        db: sqlite3.Connection,
        run_id: str,
        charge_range: int | None,
        after_sequence: int = 0,
        limit: int | None = None,
    ) -> Iterator[tuple[RecordedPoint, HistoryRecord]]:
        preceding = db.execute(
            "SELECT raw FROM records WHERE run_id=? AND sequence<=? ORDER BY sequence DESC LIMIT 1",
            (run_id, after_sequence),
        ).fetchone()
        previous = self._point(preceding[0], charge_range)[1] if preceding else None
        cursor = db.execute(
            "SELECT raw FROM records WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (run_id, after_sequence, limit + 1 if limit is not None else -1),
        )
        raw_rows = (row for batch in iter(lambda: cursor.fetchmany(4096), []) for row in batch)
        first = next(raw_rows, None)
        current = self._point(first[0], charge_range) if first is not None else None
        count = 0
        while current is not None and (limit is None or count < limit):
            next_raw = next(raw_rows, None)
            following = self._point(next_raw[0], charge_range) if next_raw is not None else None
            point, record = current
            point = _bdf.correct_time(point, record, previous, following[1] if following else None)
            if previous is not None and point.test_time_second < previous.total_time_ms / 1000:
                raise StorageError(f"Unresolved time regression at sequence {point.sequence}")
            yield point, record
            previous = replace(record, total_time_ms=round(point.test_time_second * 1000))
            current = following
            count += 1

    def finalize(self, run_id: str) -> Export:
        """Publish durable Parquet before releasing working records.

        Only BDF columns are retained. Further main history fails explicitly.
        """
        with self._export_lock, self._lock:
            if self.get(run_id).state != RunState.FINISHED:
                raise StateError("only finished runs can be finalized")
            result = self.export(run_id)
            with self._transaction() as db:
                db.execute(
                    "INSERT OR REPLACE INTO archives VALUES (?, ?, ?, ?)",
                    (
                        run_id,
                        str(result.path),
                        result.record_count,
                        json.dumps(result.missing_sequence_ranges),
                    ),
                )
                db.execute("DELETE FROM records WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM opaque_records WHERE run_id=?", (run_id,))
            return result

    def export(self, run_id: str, path: str | Path | None = None) -> Export:
        """Publish a managed BDF snapshot; replacement of the run deletes this file."""
        destination = Path(path) if path is not None else self.directory / f"{run_id}.bdf.parquet"
        destination = destination.resolve()
        if not destination.name.endswith(".bdf.parquet"):
            raise ValueError("BDF Parquet paths must end in .bdf.parquet")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid4().hex + ".partial")
        with self._export_lock, self._snapshot() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            archive = db.execute("SELECT * FROM archives WHERE run_id=?", (run_id,)).fetchone()
            if archive is not None:
                source = Path(archive["path"])
                owner = db.execute("SELECT * FROM exports WHERE path=?", (str(source),)).fetchone()
                if owner is None or self._file_hash(source) != owner["sha256"]:
                    raise StorageError("finalized export was replaced by another file")
                if destination != source:
                    existing_digest = None
                    with self._transaction() as write_db:
                        if (
                            write_db.execute(
                                "SELECT 1 FROM pending_deletions WHERE path=?", (str(destination),)
                            ).fetchone()
                            is not None
                        ):
                            raise StorageError("export path is pending deletion")
                        owner = write_db.execute(
                            "SELECT * FROM exports WHERE path=?", (str(destination),)
                        ).fetchone()
                        if owner is not None and owner["run_id"] != run_id:
                            raise StorageError("export path belongs to another run")
                        if destination.exists():
                            existing_digest = self._file_hash(destination)
                            if owner is None or existing_digest not in (
                                owner["sha256"],
                                owner["previous_sha256"],
                            ):
                                raise StorageError("export path contains another file")
                    try:
                        shutil.copyfile(source, temporary)
                        with temporary.open("r+b") as stream:
                            os.fsync(stream.fileno())
                        digest = self._file_hash(temporary)
                        with self._transaction() as write_db:
                            write_db.execute(
                                "INSERT OR REPLACE INTO exports VALUES (?, ?, ?, ?)",
                                (str(destination), run_id, digest, existing_digest),
                            )
                        os.replace(temporary, destination)
                        with self._transaction() as write_db:
                            write_db.execute(
                                "UPDATE exports SET previous_sha256=NULL WHERE path=?",
                                (str(destination),),
                            )
                    finally:
                        temporary.unlink(missing_ok=True)
                return Export(
                    destination,
                    self._run(row, archive["record_count"], bool(json.loads(archive["gaps"]))),
                    archive["record_count"],
                    tuple(tuple(gap) for gap in json.loads(archive["gaps"])),
                )
            total = db.execute("SELECT count(*) FROM records WHERE run_id=?", (run_id,)).fetchone()[
                0
            ]
            run = self._run(row, total)
            existing_digest = None
            with self._transaction() as write_db:
                pending = write_db.execute(
                    "SELECT 1 FROM pending_deletions WHERE path=?", (str(destination),)
                ).fetchone()
                if pending is not None:
                    raise StorageError("export path is pending deletion")
                owner = write_db.execute(
                    "SELECT * FROM exports WHERE path=?", (str(destination),)
                ).fetchone()
                if owner is not None and owner["run_id"] != run_id:
                    raise StorageError("export path belongs to another run")
                if destination.exists():
                    existing_digest = self._file_hash(destination)
                    if owner is None or existing_digest not in (
                        owner["sha256"],
                        owner["previous_sha256"],
                    ):
                        raise StorageError("export path contains another file")
                write_db.execute(
                    "INSERT OR IGNORE INTO exports (path, run_id) VALUES (?, ?)",
                    (str(destination), run_id),
                )
            charge_range = row["charge_range"]
            gaps: list[tuple[int, int]] = []
            previous_sequence = 0
            previous_cycle = -1
            previous_occurrence = None
            step_record_index = 0
            counters = CounterTotals(charge_range)
            count = 0
            try:
                schema = _bdf.SCHEMA
                with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
                    output = []
                    for point, record in self._points(db, run_id, charge_range):
                        if point.sequence > previous_sequence + 1:
                            gaps.append((previous_sequence + 1, point.sequence - 1))
                        previous_sequence = point.sequence
                        if point.cycle < previous_cycle:
                            raise StorageError(
                                f"Cycle count regresses at sequence {point.sequence}"
                            )
                        previous_cycle = point.cycle
                        occurrence = _bdf.occurrence(record)
                        step_record_index = (
                            step_record_index + 1 if occurrence == previous_occurrence else 1
                        )
                        previous_occurrence = occurrence
                        output.append(
                            {
                                **_bdf.row(point, record, step_record_index),
                                **counters.update(record),
                            }
                        )
                        count += 1
                        if len(output) == 4096:
                            writer.write_table(pa.Table.from_pylist(output, schema=schema))
                            output.clear()
                    if output:
                        writer.write_table(pa.Table.from_pylist(output, schema=schema))
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                digest = self._file_hash(temporary)
                with self._transaction() as write_db:
                    write_db.execute(
                        "UPDATE exports SET previous_sha256=?, sha256=? WHERE path=?",
                        (existing_digest, digest, str(destination)),
                    )
                os.replace(temporary, destination)
                with self._transaction() as write_db:
                    write_db.execute(
                        "UPDATE exports SET previous_sha256=NULL WHERE path=?", (str(destination),)
                    )
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        logger.info(
            "BDF export published run_id=%s path=%s rows=%d gap_count=%d",
            run_id,
            destination,
            count,
            len(gaps),
        )
        return Export(
            destination,
            replace(run, completeness=DataCompleteness.GAPS if gaps else DataCompleteness.UNKNOWN),
            count,
            tuple(gaps),
        )

    def checkpoint(self) -> int | None:
        with self._transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='history_token'").fetchone()
            return None if row is None else int(row[0])

    def save_checkpoint(self, token: int) -> None:
        bounded_integer("history token", token, 2**32 - 1)
        with self._transaction() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('history_token', ?)", (str(token),))
