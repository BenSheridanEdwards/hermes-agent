"""Pure stdio forwarding. Do not import agent, provider or session-store modules here."""
import asyncio
import os
import sys

from acp_adapter.local_transport import MAX_FRAME, check_private, endpoint, peer_uid
from hermes_constants import get_hermes_home


async def forward():
    path = endpoint(get_hermes_home())
    check_private(path.parent, directory=True)
    check_private(path)
    reader, writer = await asyncio.open_unix_connection(path, limit=MAX_FRAME)
    if peer_uid(writer.get_extra_info("socket")) != os.getuid():
        writer.close()
        raise PermissionError("ACP gateway peer is not the current user")
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=MAX_FRAME)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin.buffer)

    async def upstream():
        while line := await stdin.readline():
            if len(line) > MAX_FRAME:
                raise ValueError("ACP frame too large")
            writer.write(line)
            await writer.drain()

    async def downstream():
        while line := await reader.readline():
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        writer.close()
        await writer.wait_closed()


def main():
    try:
        asyncio.run(forward())
    except (OSError, ValueError) as exc:
        print(f"Cannot attach to canonical gateway: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
