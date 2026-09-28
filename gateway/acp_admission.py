"""Durable, bounded prompt admission identities; no model resurrection on restart."""
import asyncio
from contextlib import closing
import hashlib
import json
import os
import re
import sqlite3
import uuid

MAX_ADMISSIONS = 4096


class AdmissionStore:
    def __init__(self, directory):
        self.path = directory / "acp-admissions.sqlite"
        self.epoch = uuid.uuid4().hex

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS admissions (session TEXT, admission TEXT, digest TEXT, epoch TEXT, receipt TEXT, PRIMARY KEY(session, admission))")
        return db

    def get(self, sid, ident, prompt=None):
        with closing(self.connect()) as db:
            row = db.execute("SELECT digest, epoch, receipt FROM admissions WHERE session=? AND admission=?", (sid, ident)).fetchone()
        if row is None:
            return None
        if prompt is not None and row[0] != self.digest(prompt):
            raise ValueError("admission_conflict: identity already used for different prompt")
        receipt = json.loads(row[2])
        if row[1] != self.epoch and receipt["status"] in {"pending", "in_progress"}:
            receipt.update(status="unknown", error="owner_restarted: outcome ambiguous; do not resubmit")
        return receipt

    @staticmethod
    def digest(prompt):
        return hashlib.sha256(json.dumps(prompt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def claim(self, sid, ident, prompt):
        receipt = dict(sessionId=sid, admissionId=ident, turnId=uuid.uuid4().hex, status="pending")
        with closing(self.connect()) as db, db:
            if db.execute("SELECT count(*) FROM admissions").fetchone()[0] >= MAX_ADMISSIONS:
                raise ValueError("admission_capacity: retained identities full; operator action required")
            db.execute("INSERT INTO admissions VALUES (?, ?, ?, ?, ?)",
                       (sid, ident, self.digest(prompt), self.epoch, json.dumps(receipt)))
        return receipt

    def update(self, receipt, **changes):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = json.loads(db.execute("SELECT receipt FROM admissions WHERE session=? AND admission=?",
                (receipt["sessionId"], receipt["admissionId"])).fetchone()[0])
            if changes.get("status") == "in_progress" and previous["status"] not in {"pending", "in_progress"}:
                return previous
            updated = {**previous, **changes}
            db.execute("UPDATE admissions SET receipt=? WHERE session=? AND admission=?",
                       (json.dumps(updated), receipt["sessionId"], receipt["admissionId"]))
        return updated


def negotiated(emit):
    return getattr(getattr(emit, "connection", emit), "hermes_attachment", False) is True


class AdmissionMethods:
    @staticmethod
    def check_admission(params, emit):
        if not negotiated(emit):
            raise ValueError("Negotiate clientCapabilities._meta.hermesAttachment version 1 first")
        ident = params.get("admissionId")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", ident):
            raise ValueError("admissionId must be 1..128 ASCII identifier characters")
        return ident

    async def turn_status(self, params, emit):
        ident = self.check_admission(params, emit)
        await self.entry(params.get("sessionId"))
        result = await asyncio.to_thread(self.admissions.get, params["sessionId"], ident)
        return result or {"sessionId": params["sessionId"], "admissionId": ident, "status": "not_found"}

    async def admit(self, params, emit):
        ident = self.check_admission(params, emit)
        await self.entry(params.get("sessionId"))
        prompt = params.get("prompt")
        if not isinstance(prompt, list) or not prompt or any(
                not isinstance(p, dict) or p.get("type") != "text" or not isinstance(p.get("text"), str) for p in prompt):
            raise ValueError("Attachment prompts support text blocks only")
        if "\n".join(p["text"] for p in prompt).lstrip().startswith("/"):
            raise ValueError("Admission is for new model work, not control commands")
        async with self.admission_lock:
            previous = await asyncio.to_thread(self.admissions.get, params["sessionId"], ident, prompt)
            if previous is not None:
                return previous
            receipt = await asyncio.to_thread(self.admissions.claim, params["sessionId"], ident, prompt)
            try:
                return await self.prompt(params, emit, admission=receipt)
            except Exception as exc:
                await asyncio.to_thread(self.admissions.update, receipt, status="rejected", error=str(exc)[:1024])
                raise
