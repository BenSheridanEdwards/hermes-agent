"""Bounded durable ACP notices, independent of cached model history."""
import json
import os
import sqlite3
from contextlib import closing

MAX_ROWS = 4096
MAX_BYTES = 16 * 1024 * 1024
PAGE_BYTES = 512 * 1024
PAGE_ROWS = 64


def delivery_frame(ident, message, *, replay=False):
    params = message.get("params", {})
    return {**message, "params": {**params, "_meta": {
        **params.get("_meta", {}), "deliveryId": ident, "replay": replay}}}


class DeliveryJournal:
    def __init__(self, directory):
        self.path = directory / "acp-delivery.sqlite"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, message TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS deliveries_session ON deliveries(session_id, id)")
        db.execute("CREATE TABLE IF NOT EXISTS retention (singleton INTEGER PRIMARY KEY CHECK(singleton=1), floor INTEGER NOT NULL)")
        db.execute("INSERT OR IGNORE INTO retention VALUES (1, 0)")
        db.commit()
        return db

    def checkpoint(self):
        with closing(self._connect()) as db:
            return db.execute("SELECT max(coalesce((SELECT max(id) FROM deliveries), 0), floor) FROM retention WHERE singleton=1").fetchone()[0]

    def append(self, session_id, message):
        from acp_adapter.local_transport import encode
        encoded = encode(delivery_frame(9223372036854775807, message, replay=True))
        if len(encoded) > 240 * 1024:
            raise ValueError("ACP delivery too large (240 KiB limit)")
        payload = json.dumps(message, ensure_ascii=False)
        with closing(self._connect()) as db, db:
            ident = db.execute("INSERT INTO deliveries(session_id, message) VALUES (?, ?)",
                               (session_id, payload)).lastrowid
            rows = db.execute("SELECT id, length(CAST(message AS BLOB)) FROM deliveries ORDER BY id DESC").fetchall()
            size, cutoff = 0, None
            for index, (row_id, length) in enumerate(rows):
                size += length
                if index >= MAX_ROWS or size > MAX_BYTES:
                    cutoff = row_id
                    break
            if cutoff is not None:
                db.execute("UPDATE retention SET floor=max(floor, ?) WHERE singleton=1", (cutoff,))
                db.execute("DELETE FROM deliveries WHERE id<=?", (cutoff,))
        return ident

    def read(self, session_id, after=0, limit=PAGE_ROWS):
        from acp_adapter.local_transport import encode
        with closing(self._connect()) as db:
            floor = db.execute("SELECT floor FROM retention WHERE singleton=1").fetchone()[0]
            if after < floor:
                raise ValueError(f"replay_gap: cursor {after} precedes retention floor {floor}")
            rows = db.execute("SELECT id, message FROM deliveries WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                              (session_id, after, min(limit, PAGE_ROWS) + 1)).fetchall()
        result, size = [], 0
        for ident, payload in rows[:min(limit, PAGE_ROWS)]:
            message = json.loads(payload)
            try:
                length = len(encode(delivery_frame(ident, message, replay=True)))
            except ValueError:
                raise ValueError("replay_gap: legacy oversized delivery requires operator repair") from None
            if result and size + length > PAGE_BYTES:
                break
            size += length
            result.append((ident, message))
        return result, len(rows) > len(result)
