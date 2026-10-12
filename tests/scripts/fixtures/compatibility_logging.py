"""Keep pytest's capture handlers active while owning model logging resources."""
from contextlib import contextmanager
import logging

from tests.scripts.fixtures.scratch_liveness_probe import isolated_logging_state


@contextmanager
def compatibility_logging_state():
    import hermes_logging as logs

    root = logging.getLogger()
    external = [handler for handler in root.handlers if not getattr(handler, '_hermes_queue', False)]
    session = dict(vars(logs._session_context))
    with isolated_logging_state():
        for handler in external:
            root.addHandler(handler)
        try:
            yield
        finally:
            logs.flush_log_queue()
            for handler in list(root.handlers):
                if not getattr(handler, '_hermes_queue', False):
                    root.removeHandler(handler)
                    # Only setup_verbose_logging's fixture-owned handler is
                    # ours to close. Pytest owns its dynamic capture handlers.
                    if getattr(handler, '_hermes_verbose', False) and handler not in external:
                        handler.close()
            vars(logs._session_context).clear()
            vars(logs._session_context).update(session)
