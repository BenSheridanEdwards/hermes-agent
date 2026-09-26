"""Bounded, same-user Unix transport for the canonical gateway ACP endpoint.

Only Linux and macOS are supported; no TCP or independent-agent fallback.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys

MAX_FRAME = 256 * 1024
MAX_CLIENTS = 8
MAX_PENDING = 8
MAX_REQUESTS = 64
QUEUE_SIZE = 64


def peer_uid(sock):
    if sys.platform == "linux":
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), "getpeereid failed")
        return uid.value
    raise OSError("ACP attachment supports Linux and macOS only")


def endpoint(home):
    return Path(home) / "acp" / "gateway.sock"


def check_private(path, *, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISSOCK
    if not expected(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"Unsafe ACP endpoint: {path}")


def encode(message):
    data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ValueError("ACP frame too large")
    return data


class ACPListener:
    def __init__(self, home, dispatch):
        self.path = endpoint(home)
        self.dispatch = dispatch
        self.server = None
        self._identity = None
        self._lock_fd = None
        self.clients = set()
        self.tasks = set()

    async def start(self):
        if sys.platform not in {"darwin", "linux"}:
            raise OSError("ACP attachment supports Linux and macOS only")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_private(self.path.parent, directory=True)
        import fcntl

        # A persistent, kernel-held lease survives as evidence after SIGKILL.
        # Never unlink the lock file: contenders must lock the same inode.
        lock_path = self.path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise PermissionError("Unsafe ACP ownership lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise FileExistsError(f"ACP endpoint already owned: {self.path}") from None
            if lock_path.lstat() != info:
                raise PermissionError("ACP ownership lock replaced")
            if self.path.exists() or self.path.is_symlink():
                check_private(self.path)
                previous = self.path.lstat()
                identity = f"{previous.st_dev}:{previous.st_ino}".encode()
                if os.read(fd, 128) != identity:
                    raise FileExistsError(f"Unowned ACP endpoint: {self.path}")
                # Even valid crash evidence cannot authorize removal of a live listener.
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise
                    else:
                        raise FileExistsError(f"ACP endpoint already live: {self.path}")
                if self.path.lstat() != previous:
                    raise PermissionError("ACP endpoint replaced during recovery")
                self.path.unlink()
            sock.setblocking(False)
            sock.bind(str(self.path))  # unlike start_unix_server(path=), never unlinks a live socket
            os.chmod(self.path, 0o600)
            bound = self.path.lstat()
            self._identity = (bound.st_dev, bound.st_ino)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{bound.st_dev}:{bound.st_ino}".encode())
            self.server = await asyncio.start_unix_server(self._accept, sock=sock, limit=MAX_FRAME)
            self._lock_fd = fd
        except BaseException:
            sock.close()
            os.close(fd)
            raise

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.clients):
            writer.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            info = None
        if (info is not None and stat.S_ISSOCK(info.st_mode)
                and (info.st_dev, info.st_ino) == self._identity):
            self.path.unlink()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self._identity = None

    async def _accept(self, reader, writer):
        if len(self.clients) >= MAX_CLIENTS or peer_uid(writer.get_extra_info("socket")) != os.getuid():
            writer.close()
            return
        self.clients.add(writer)
        queue = asyncio.Queue(QUEUE_SIZE)
        pending = set()

        def emit(message):
            if writer.is_closing():
                return
            try:
                if queue.qsize() >= QUEUE_SIZE - MAX_PENDING:
                    raise asyncio.QueueFull
                queue.put_nowait(encode(message))
            except (asyncio.QueueFull, ValueError):
                # A subscriber must never block the canonical model worker.
                writer.close()

        emit.closed = writer.is_closing

        async def write():
            while True:
                writer.write(await queue.get())
                await asyncio.wait_for(writer.drain(), 5)
                queue.task_done()

        async def send(message):
            if writer.is_closing():
                raise ConnectionError("ACP subscriber disconnected")
            await asyncio.wait_for(queue.put(encode(message)), 5)
            await asyncio.sleep(0)

        async def flush():
            await asyncio.wait_for(queue.join(), 5)
            if writer.is_closing():
                raise ConnectionError("ACP subscriber disconnected")

        def reply_nowait(message):
            queue.put_nowait(encode(message))

        async def request(message):
            from acp_adapter.attachment_emitter import RequestEmitter
            scoped = RequestEmitter(emit, send, message)
            scoped.flush = flush
            scoped.reply_nowait = reply_nowait
            response = {"jsonrpc": "2.0", "id": message.get("id")}
            try:
                response["result"] = await self.dispatch(message, scoped)
            except Exception as exc:
                response["error"] = {"code": -32000, "message": str(exc)}
            if "id" in message and not scoped.replied:
                try:
                    await send(response)
                except (ConnectionError, asyncio.TimeoutError):
                    writer.close()

        async def read():
            while line := await reader.readline():
                if len(line) > MAX_FRAME or len(pending) >= MAX_PENDING or len(self.tasks) >= MAX_REQUESTS:
                    break
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    break
                task = asyncio.create_task(request(message))
                pending.add(task)
                self.tasks.add(task)
                task.add_done_callback(pending.discard)
                task.add_done_callback(self.tasks.discard)

        pump = asyncio.create_task(write())
        receive = asyncio.create_task(read())
        try:
            # Either direction ending tears down the subscriber, not its requests.
            await asyncio.wait((pump, receive), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Graceful close can wait forever for a non-reading client's buffer.
            writer.transport.abort()
            pump.cancel()
            receive.cancel()
            await asyncio.gather(pump, receive, return_exceptions=True)
            self.clients.discard(writer)
            owner = getattr(self.dispatch, "__self__", None)
            if owner is not None and hasattr(owner, "detach"):
                owner.detach(emit)
