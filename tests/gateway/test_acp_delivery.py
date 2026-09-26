import asyncio

import pytest

from gateway.acp_delivery import DeliveryJournal


def test_delivery_journal_reopens_and_paginates_without_cross_session_rows(tmp_path):
    journal = DeliveryJournal(tmp_path)
    ids = []
    for index in range(70):
        ids.append(journal.append("owner", {"number": index}))
    journal.append("other", {"number": "private"})
    journal = DeliveryJournal(tmp_path)
    first, more = journal.read("owner")
    second, more_after = journal.read("owner", after=first[-1][0])
    assert more is True and more_after is False
    assert [ident for ident, _ in first + second] == ids
    assert [row["number"] for _, row in first + second] == list(range(70))
    assert journal.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_storage_error_is_not_delivery_success(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway.acp_bridge import LocalAttachmentAdapter, GatewayACPBridge
    from gateway.config import Platform
    from gateway.session import SessionSource
    entry = SimpleNamespace(session_id="sid", session_key="key")
    async def lookup(key):
        assert key == "key"
        return entry
    runner = SimpleNamespace(config=SimpleNamespace(sessions_dir=tmp_path),
                             async_session_store=SimpleNamespace(lookup_by_session_key=lookup),
                             _session_key_for_source=lambda source: "key")
    bridge = GatewayACPBridge(runner)
    observed = []
    bridge.subscribers["sid"] = observed.append
    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(bridge.journal, "append", broken)
    adapter = LocalAttachmentAdapter()
    adapter.gateway_runner = runner
    result = await adapter.send("chat", "Must not be silently dropped")
    assert result.success is False and "disk full" in result.error
    assert observed == []
