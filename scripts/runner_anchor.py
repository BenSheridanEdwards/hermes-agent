"""Private Darwin live leader for the canonical process owner.

Imported definitions have no process effects. Only the checked-in entrypoint
launches the already validated worker; its private control FD is never inherited
by that worker. Worker stdio and the registered-owner endpoint are unchanged.
"""
from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time

MAX_FRAME = 65536
HANDSHAKE_SECONDS = 5.0
CLOSURE_SECONDS = 10.0


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate anchor protocol key')
        value[key] = item
    return value


def _read_exact(connection, size, deadline, *, allow_eof=False):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([connection], [], [], remaining)[0]:
            raise TimeoutError('anchor protocol deadline')
        chunk = connection.recv(size - len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise EOFError('truncated anchor protocol frame')
        data.extend(chunk)
    return bytes(data)


def receive(connection, deadline):
    header = _read_exact(connection, 4, deadline, allow_eof=True)
    if header is None:
        return None
    size, = struct.unpack('!I', header)
    if not 0 < size <= MAX_FRAME:
        raise ValueError('anchor protocol frame size')
    raw = _read_exact(connection, size, deadline)
    if raw is None:
        raise EOFError('missing anchor protocol payload')
    value = json.loads(raw, object_pairs_hook=_unique)
    if type(value) is not dict:
        raise ValueError('anchor protocol object required')
    return value


def send(connection, value, deadline):
    raw = json.dumps(value, allow_nan=False, separators=(',', ':')).encode()
    if not 0 < len(raw) <= MAX_FRAME:
        raise ValueError('anchor protocol frame size')
    data = memoryview(struct.pack('!I', len(raw)) + raw)
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [connection], [], remaining)[1]:
            raise TimeoutError('anchor protocol send deadline')
        written = connection.send(data)
        if written <= 0:
            raise BrokenPipeError('anchor control closed')
        data = data[written:]


class Protocol:
    """One producer, one ordered finite transcript. No inferred worker result."""

    def __init__(self, pid):
        self.pid = pid
        self.ready = False
        self.worker_pid = None
        self.returncode = None
        self.close_requested = False
        self.closing = False

    def accept(self, message):
        kind = message.get('type')
        if self.closing:
            raise ValueError('anchor record after closing acknowledgement')
        if kind == 'error' and set(message) == {'type', 'error'} and type(message['error']) is str:
            raise RuntimeError('anchor failure: ' + message['error'][:512])
        if not self.ready:
            if message != {'type': 'ready', 'pid': self.pid} or type(message.get('pid')) is not int:
                raise ValueError('anchor startup identity/order')
            self.ready = True
            return
        if self.worker_pid is None:
            if (set(message) != {'type', 'pid'} or kind != 'started'
                    or type(message['pid']) is not int or message['pid'] <= 0 or message['pid'] == self.pid):
                raise ValueError('anchor worker identity/order')
            self.worker_pid = message['pid']
            return
        if kind == 'result':
            if (set(message) != {'type', 'pid', 'returncode'} or self.returncode is not None
                    or type(message['pid']) is not int or message['pid'] != self.worker_pid
                    or type(message['returncode']) is not int or not -127 <= message['returncode'] <= 255):
                raise ValueError('anchor worker result identity/value/duplication')
            self.returncode = message['returncode']
            return
        if (kind == 'closing' and set(message) == {'type', 'pid', 'result_sent'}
                and type(message['pid']) is int and message['pid'] == self.pid
                and type(message['result_sent']) is bool and self.close_requested
                and message['result_sent'] == (self.returncode is not None)):
            self.closing = True
            return
        raise ValueError('anchor closure order or malformed record')


class AnchorChannel:
    """Single reader allows ancestor cancellation while run awaits a result."""

    def __init__(self, deadline):
        self.connection, self.child = socket.socketpair()
        try:
            self.connection.setblocking(False)
            self.child.setblocking(False)
        except BaseException:
            self.connection.close()
            self.child.close()
            raise
        self.deadline = deadline
        self.hard_deadline = deadline + CLOSURE_SECONDS
        self.condition = threading.Condition()
        self.protocol = None
        self.reader = None
        self.error = None
        self.eof = False

    @property
    def worker_pid(self):
        return self.protocol.worker_pid if self.protocol else None

    @property
    def returncode(self):
        return self.protocol.returncode if self.protocol else None

    def _read(self):
        try:
            assert self.protocol is not None
            while True:
                message = receive(self.connection, self.hard_deadline)
                with self.condition:
                    if message is None:
                        self.eof = True
                        if not self.protocol.closing:
                            raise EOFError('anchor died before acknowledged cleanup')
                        self.condition.notify_all()
                        return
                    self.protocol.accept(message)
                    self.condition.notify_all()
        except BaseException as error:
            with self.condition:
                self.error = error
                self.condition.notify_all()

    def _wait(self, predicate, deadline):
        with self.condition:
            while True:
                if self.error is not None:
                    raise self.error
                if predicate():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('anchor handshake/result deadline')
                self.condition.wait(remaining)

    def start(self, pid, cmd):
        self.child.close()
        self.protocol = Protocol(pid)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        until = min(self.deadline, time.monotonic() + HANDSHAKE_SECONDS)
        self._wait(lambda: self.protocol is not None and self.protocol.ready, until)
        send(self.connection, {'type': 'launch', 'argv': cmd, 'deadline': self.deadline}, until)
        self._wait(lambda: self.worker_pid is not None, until)
        return self.worker_pid

    def wait_result(self):
        self._wait(lambda: self.returncode is not None or (
            self.protocol is not None and self.protocol.closing), self.deadline)
        return self.returncode

    def prepare_close(self, deadline):
        with self.condition:
            if self.error is not None:
                raise self.error
            if self.protocol is None or self.worker_pid is None:
                raise RuntimeError('anchor startup not acknowledged')
            self.protocol.close_requested = True
        send(self.connection, {'type': 'close'}, deadline)
        self._wait(lambda: self.protocol is not None and self.protocol.closing, deadline)

    def finish(self, deadline):
        self._wait(lambda: self.eof, deadline)
        if self.protocol is None or not self.protocol.closing:
            raise RuntimeError('anchor closure not acknowledged')

    def dispose(self, deadline):
        self.child.close()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError as error:
            if error.errno not in (errno.ENOTCONN, errno.EBADF):
                raise
        self.connection.close()
        if self.reader is not None:
            self.reader.join(timeout=max(0, deadline - time.monotonic()))
            if self.reader.is_alive():
                raise RuntimeError('anchor protocol reader did not close')


def worker_command(argv):
    """Keep the canonical command and rebind its receipt to its actual parent."""
    root = Path(__file__).resolve().parent.parent
    if not isinstance(argv, list) or not argv or any(type(arg) is not str for arg in argv):
        raise ValueError('invalid canonical worker argv')
    argv = list(argv)
    if argv[:3] == [sys.executable, '-m', 'pytest']:
        sys.path.insert(0, str(root))
        from scripts.runner_scratch import receive_worker, worker_receipt
        receipts = [i for i, arg in enumerate(argv) if arg.startswith('--runner-owned-scratch=')]
        if len(receipts) != 1:
            raise ValueError('anchor requires one owned pytest receipt')
        index = receipts[0]
        lease = receive_worker(argv[index].split('=', 1)[1])
        argv[index] = '--runner-owned-scratch=' + worker_receipt(lease)
    else:
        import shutil
        bash = shutil.which('bash')
        if (not bash or Path(argv[0]).resolve() != Path(bash).resolve() or len(argv) < 4
                or argv[1:3] != [str(root / 'scripts/run_tests.sh'), '--scratch-root']):
            raise ValueError('anchor accepts only canonical owned launches')
    return argv


class ParentExitWatch:
    """Darwin kernel subscription bound while our actual parent still exists.

    EOF alone cannot distinguish parent death from a live owner's failed cleanup.
    getppid after EOF also races reparenting. NOTE_EXIT avoids both inferences.
    """

    def __init__(self, parent):
        self.parent = parent
        self.queue = select.kqueue()
        try:
            self.queue.control([select.kevent(parent, filter=select.KQ_FILTER_PROC,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_ONESHOT,
                               fflags=select.KQ_NOTE_EXIT)], 0, 0)
        except BaseException:
            self.queue.close()
            raise

    def exited(self, deadline):
        events = self.queue.control(None, 1, max(0, deadline - time.monotonic()))
        if not events:
            return False
        event = events[0]
        if (event.ident != self.parent or event.filter != select.KQ_FILTER_PROC
                or event.flags & select.KQ_EV_ERROR or not event.fflags & select.KQ_NOTE_EXIT):
            raise RuntimeError('invalid parent exit event')
        return True

    def close(self):
        self.queue.close()


def serve_anchor(connection, owner_fd):
    """Own a live session, never substitute its status for the actual worker."""
    pid, parent = os.getpid(), os.getppid()
    if os.getsid(0) != pid or os.getpgid(0) != pid:  # windows-footgun: ok - Darwin internal helper
        raise RuntimeError('anchor must be its new session/group leader')
    until = time.monotonic() + HANDSHAKE_SECONDS
    hard_deadline = until + CLOSURE_SECONDS
    send_lock = threading.Lock()
    state = {'closing': False, 'result_sent': False}
    owner_open = True
    parent_watch = None
    try:
        parent_watch = ParentExitWatch(parent)
        send(connection, {'type': 'ready', 'pid': pid}, until)
        launch = receive(connection, until)
        if (type(launch) is not dict or set(launch) != {'type', 'argv', 'deadline'}
                or launch['type'] != 'launch' or type(launch['deadline']) not in (int, float)
                or not math.isfinite(launch['deadline']) or launch['deadline'] <= time.monotonic()):
            raise ValueError('invalid anchor launch handshake/deadline')
        deadline = launch['deadline']
        hard_deadline = deadline + CLOSURE_SECONDS
        cmd = worker_command(launch['argv'])
        worker = subprocess.Popen(cmd, close_fds=True, pass_fds=(owner_fd,),
                                  stdin=0, stdout=1, stderr=2, start_new_session=False)
        os.close(owner_fd)
        owner_open = False
        for fd in (0, 1, 2):
            os.close(fd)  # Only worker descendants now hold the original stream ends.
        send(connection, {'type': 'started', 'pid': worker.pid}, min(deadline, until))

        def observe_worker():
            try:
                # Retain the worker PID as well as the anchor PID. Nested receipt
                # parentage must not be rebound to a reused worker PID.
                result = os.waitid(os.P_PID, worker.pid, os.WEXITED | os.WNOWAIT)
                if result is None or result.si_pid != worker.pid or result.si_code not in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED):
                    raise RuntimeError('invalid anchor worker exit witness')
                rc = result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
                with send_lock:
                    if not state['closing']:
                        send(connection, {'type': 'result', 'pid': worker.pid, 'returncode': rc}, hard_deadline)
                        state['result_sent'] = True
            except BaseException as error:
                with send_lock:
                    if not state['closing']:
                        send(connection, {'type': 'error', 'error': repr(error)[:512]}, hard_deadline)

        threading.Thread(target=observe_worker, daemon=True).start()
        request = receive(connection, hard_deadline)
        if request != {'type': 'close'}:
            raise ValueError('missing/malformed anchor closure request')
        with send_lock:
            state['closing'] = True
            send(connection, {'type': 'closing', 'pid': pid,
                              'result_sent': state['result_sent']}, hard_deadline)
        # ACK does not exit the anchor. Only the legitimate owner group signal
        # completes normal cleanup. No duplicate command/result is acceptable.
        if receive(connection, hard_deadline) is not None:
            raise ValueError('unexpected record after anchor closure acknowledgement')
    except BaseException as error:
        try:
            with send_lock:
                send(connection, {'type': 'error', 'error': repr(error)[:512]}, hard_deadline)
        except (OSError, ValueError, TimeoutError):
            pass  # Transport failure is already a fail-closed owner outcome.
        # Stay a live signal target after a reported startup/result error. The
        # owner can still reclaim this owned group, but cannot delete storage.
        try:
            receive(connection, hard_deadline)
        except (OSError, ValueError, TimeoutError, EOFError):
            pass
    finally:
        if owner_open:
            os.close(owner_fd)
        try:
            # Keep the anchor live for the owner to reclaim even after IPC loss.
            # Only a kernel-confirmed creator exit permits self-group cancellation;
            # neither EOF nor the bounded wait expiring retries an owner denial.
            if parent_watch is not None and parent_watch.exited(hard_deadline):
                import signal
                if os.getsid(0) != pid or os.getpgid(0) != pid:  # windows-footgun: ok - own Darwin session
                    raise RuntimeError('anchor lost its own session identity')
                os.killpg(pid, signal.SIGKILL)  # windows-footgun: ok - confirmed parent death, own group only
        finally:
            if parent_watch is not None:
                parent_watch.close()
            connection.close()
    return 125  # Never an ordinary successful anchor exit.


def main():
    if sys.platform != 'darwin' or len(sys.argv) != 3:
        raise RuntimeError('private Darwin anchor requires inherited control/owner descriptors')
    control_fd, owner_fd = map(int, sys.argv[1:])
    if control_fd < 3 or owner_fd < 3 or control_fd == owner_fd:
        raise ValueError('invalid anchor descriptors')
    connection = socket.socket(fileno=control_fd)
    connection.setblocking(False)
    return serve_anchor(connection, owner_fd)


if __name__ == '__main__':
    raise SystemExit(main())
