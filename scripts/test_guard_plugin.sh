# Canonical test-wrapper discovery. Argument is a lookup base, not a HOME override.
# Kept separate so absent/present behavior can use inert fixtures without touching
# the operator's profile or executing its plugin. The wrapper always passes $HOME.
hermes_test_guard_plugin() {
  EXTRA_PYTHONPATH=""
  EXTRA_PYTEST_PLUGINS=""
  if [ -f "$1/.hermes/pytest_live_guard.py" ]; then
    EXTRA_PYTHONPATH="$1/.hermes"
    EXTRA_PYTEST_PLUGINS="pytest_live_guard"
  fi
}
