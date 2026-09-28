"""Real Unix socket and stdio subprocess contracts (no model credentials)."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_forwarder_streams_until_terminal_and_disconnect_leaves_server_alive():
    from acp_adapter.local_transport import ACPListener

    async def dispatch(request, emit):
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"text": "first"}})
        await asyncio.sleep(0)
        return {"owner": os.getpid()}

    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        home = Path(directory)
        listener = ACPListener(home, dispatch)
        await listener.start()
        try:
            for _ in range(2):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "acp_adapter.attach",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env={**os.environ, "HERMES_HOME": str(home)},
                )
                proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
                await proc.stdin.drain()
                update = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
                result = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
                assert update["method"] == "session/update"
                assert result == {"jsonrpc": "2.0", "id": 1, "result": {"owner": os.getpid()}}
                proc.stdin.close()
                assert await asyncio.wait_for(proc.wait(), 10) == 0
            assert listener.path.stat().st_mode & 0o777 == 0o600
        finally:
            await listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "live_socket", "stale_socket", "symlink"])
async def test_crashed_listener_endpoint_is_recovered(replacement):
    from acp_adapter.local_transport import ACPListener

    async def dispatch(request, emit):
        return {"restarted": True}

    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import asyncio, os\n"
            "from acp_adapter.local_transport import ACPListener\n"
            "async def main():\n"
            "    listener = ACPListener(os.environ['HERMES_HOME'], None)\n"
            "    await listener.start()\n"
            "    print('ready', flush=True)\n"
            "    await asyncio.Event().wait()\n"
            "asyncio.run(main())\n",
            stdout=asyncio.subprocess.PIPE,
            env={**os.environ, "HERMES_HOME": directory},
        )
        try:
            assert proc.stdout is not None
            assert await asyncio.wait_for(proc.stdout.readline(), 5) == b"ready\n"
        finally:
            proc.kill()
            await proc.wait()
        listener = ACPListener(Path(directory), dispatch)
        assert listener.path.exists()
        if replacement:
            import socket

            # Keep the old inode allocated so replacement cannot masquerade via reuse.
            original = listener.path.with_name("original.sock")
            listener.path.rename(original)
            other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if replacement == "symlink":
                    listener.path.symlink_to(original)
                else:
                    other.bind(str(listener.path))
                    listener.path.chmod(0o600)
                    if replacement == "live_socket":
                        other.listen()
                    else:
                        other.close()
                before = listener.path.lstat()
                with pytest.raises((FileExistsError, PermissionError)):
                    await listener.start()
                await listener.close()
                assert listener.path.lstat() == before
                assert original.exists()
            finally:
                other.close()
            return
        try:
            await listener.start()
            reader, writer = await asyncio.open_unix_connection(listener.path)
            try:
                writer.write(b'{"jsonrpc":"2.0","id":1}\n')
                await writer.drain()
                result = json.loads(await asyncio.wait_for(reader.readline(), 5))
                assert result["result"] == {"restarted": True}
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_accepting", [False, True])
async def test_existing_listener_cannot_be_replaced(stop_accepting):
    from acp_adapter.local_transport import ACPListener
    async def dispatch(request, emit):
        return {}
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = ACPListener(Path(directory), dispatch)
        other = ACPListener(Path(directory), dispatch)
        await listener.start()
        inode = listener.path.stat().st_ino
        if stop_accepting:
            # ECONNREFUSED alone is not proof that the endpoint owner has exited.
            assert listener.server is not None
            listener.server.close()
            await listener.server.wait_closed()
        try:
            with pytest.raises(FileExistsError):
                await other.start()
            assert listener.path.stat().st_ino == inode
        finally:
            await other.close()
            await listener.close()


@pytest.mark.asyncio
async def test_unauthorized_peer_and_slow_subscriber_fail_closed(monkeypatch):
    from acp_adapter import local_transport as transport
    calls = []
    async def dispatch(request, emit):
        calls.append(request)
        if request["method"] == "flood":
            for _ in range(transport.QUEUE_SIZE + 1):
                emit({"jsonrpc": "2.0", "method": "session/update", "params": {"text": "x"}})
        return {}
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = transport.ACPListener(Path(directory), dispatch)
        await listener.start()
        real_peer = transport.peer_uid
        try:
            monkeypatch.setattr(transport, "peer_uid", lambda sock: os.getuid() + 1)
            reader, writer = await asyncio.open_unix_connection(listener.path)
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
            assert calls == []
            monkeypatch.setattr(transport, "peer_uid", real_peer)
            reader, writer = await asyncio.open_unix_connection(listener.path)
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"flood"}\n')
            await writer.drain()
            await asyncio.wait_for(reader.read(), 5)
            writer.close()
            reader, writer = await asyncio.open_unix_connection(listener.path)
            writer.write(b'{"jsonrpc":"2.0","id":2,"method":"initialize"}\n')
            await writer.drain()
            assert json.loads(await asyncio.wait_for(reader.readline(), 5))["id"] == 2
            writer.close()
        finally:
            await listener.close()


@pytest.mark.asyncio
async def test_missing_gateway_forwarder_never_creates_state(tmp_path):
    before = set(tmp_path.iterdir())
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "acp_adapter.attach",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HERMES_HOME": str(tmp_path)})
    stdout, stderr = await asyncio.wait_for(proc.communicate(b""), 10)
    assert proc.returncode == 1
    assert stdout == b""
    assert b"Cannot attach to canonical gateway" in stderr
    assert set(tmp_path.iterdir()) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "connection"])
async def test_writer_failure_disconnects_without_cancelling_request(monkeypatch, failure):
    from acp_adapter import local_transport as transport

    monkeypatch.setattr(transport, "MAX_CLIENTS", 1)
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    accepted, disconnected = asyncio.Event(), asyncio.Event()

    async def dispatch(request, emit):
        entered.set()
        emit({"jsonrpc": "2.0", "method": "session/update"})
        await release.wait()
        completed.set()
        return {}

    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = transport.ACPListener(Path(directory), dispatch)
        real_accept = listener._accept

        async def accept(reader, writer):
            accepted.set()
            try:
                await real_accept(reader, writer)
            finally:
                disconnected.set()

        monkeypatch.setattr(listener, "_accept", accept)
        await listener.start()
        writer = writer2 = None
        try:
            reader, writer = await asyncio.open_unix_connection(listener.path)
            await asyncio.wait_for(accepted.wait(), 5)
            server_writer = next(iter(listener.clients))

            async def broken_drain():
                if failure == "connection":
                    raise ConnectionError("subscriber stopped reading")
                await asyncio.Event().wait()  # exercise the real pump drain timeout

            monkeypatch.setattr(server_writer, "drain", broken_drain)
            # A real blocked output buffer must be aborted, not left flushing forever.
            server_writer.write(b"x" * (4 * 1024 * 1024))
            assert server_writer.transport.get_write_buffer_size() > 0
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"wait"}\n')
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(disconnected.wait(), 8)
            await asyncio.wait_for(asyncio.shield(server_writer.wait_closed()), 2)
            await asyncio.wait_for(reader.read(), 5)  # reaches EOF, not a stuck read loop
            assert not listener.clients
            assert len(listener.tasks) == 1
            assert not completed.is_set()
            reader2, writer2 = await asyncio.open_unix_connection(listener.path)
            writer2.write(b'{"jsonrpc":"2.0","id":2,"method":"wait"}\n')
            await writer2.drain()
            assert json.loads(await asyncio.wait_for(reader2.readline(), 5))["method"] == "session/update"
            release.set()
            await asyncio.wait_for(asyncio.gather(*listener.tasks), 5)
            assert completed.is_set()
            assert json.loads(await asyncio.wait_for(reader2.readline(), 5))["id"] == 2
        finally:
            release.set()
            await asyncio.gather(*listener.tasks)
            for client in (writer, writer2):
                if client is not None:
                    client.close()
                    await client.wait_closed()
            await listener.close()


@pytest.mark.asyncio
async def test_disconnected_requests_count_toward_global_limit(monkeypatch):
    from acp_adapter import local_transport as transport
    monkeypatch.setattr(transport, "MAX_REQUESTS", 1, raising=False)
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(request, emit):
        entered.set()
        await release.wait()
        return {}
    with tempfile.TemporaryDirectory(prefix="acp-", dir="/tmp") as directory:
        listener = transport.ACPListener(Path(directory), dispatch)
        await listener.start()
        try:
            reader, writer = await asyncio.open_unix_connection(listener.path)
            writer.write(b'{"jsonrpc":"2.0","id":1,"method":"wait"}\n')
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 5)
            writer.close()
            entered.clear()
            reader2, writer2 = await asyncio.open_unix_connection(listener.path)
            writer2.write(b'{"jsonrpc":"2.0","id":2,"method":"wait"}\n')
            await writer2.drain()
            try:
                assert await asyncio.wait_for(reader2.read(), 2) == b""
                assert len(listener.tasks) == 1
            finally:
                writer2.close()
        finally:
            release.set()
            await asyncio.gather(*listener.tasks)
            await listener.close()
