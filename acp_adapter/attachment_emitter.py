"""Request-scoped response fences over a bounded connection queue."""
import asyncio


class RequestEmitter:
    def __init__(self, emit, send, request):
        self.connection = emit
        self.send = send
        self.request = request
        self.replied = False

    def __call__(self, message):
        self.connection(message)


async def replay_emit(emit, message):
    """Replay may backpressure; model callbacks must remain nonblocking."""
    if hasattr(emit, "send"):
        await emit.send(message)
    else:
        emit(message)
        await asyncio.sleep(0)
