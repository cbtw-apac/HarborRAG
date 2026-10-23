"""Process logging configuration is idempotent and stays off the root logger."""

from __future__ import annotations

import io
import logging

import pytest

from harborrag_core.observability.process_logging import (
    LEVEL_ENV_VAR,
    ROOT_LOGGER_NAME,
    configure_logging,
    resolve_level,
)


@pytest.fixture(autouse=True)
def restore_harborrag_logger():
    """Give each test a pristine "harborrag" logger, and put it back after.

    Resetting before the test as well as after is what makes these hermetic.
    ``configure_logging`` is deliberately idempotent, so a handler left on this
    shared logger by anything that ran earlier -- another package's suite
    configuring logging, or a caplog handler bound to this namespace -- made
    the call here a no-op: the test's own stream then received nothing, and the
    handler count included handlers it never installed.
    """

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    try:
        yield
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_configure_installs_one_handler_and_emits_records() -> None:
    """A configured namespace logger actually writes, which is the whole point."""

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    logging.getLogger("harborrag.runtime.example").info("worker started")

    output = stream.getvalue()
    assert "worker started" in output
    assert "test_observability_process_logging.test_configure_installs_one_handler" in output


def test_repeat_configuration_does_not_stack_handlers() -> None:
    """Two entry points configuring logging must not double every record."""

    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("DEBUG", stream=io.StringIO())

    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logging.getLogger("harborrag.app.example").info("once")

    assert len(logger.handlers) == 1
    assert stream.getvalue().count("once") == 1
    assert logger.level == logging.DEBUG


def test_propagation_stays_off_so_hosts_do_not_double_log() -> None:
    """uvicorn configures the root logger; propagating would duplicate records."""

    configure_logging("INFO", stream=io.StringIO())

    assert logging.getLogger(ROOT_LOGGER_NAME).propagate is False


def test_level_comes_from_the_environment_when_unspecified(monkeypatch) -> None:
    monkeypatch.setenv(LEVEL_ENV_VAR, "warning")

    assert resolve_level() == logging.WARNING


def test_unknown_level_falls_back_to_info_instead_of_raising(monkeypatch) -> None:
    """A typo in an operator's environment must not stop a worker from booting."""

    monkeypatch.setenv(LEVEL_ENV_VAR, "not-a-level")

    assert resolve_level() == logging.INFO


def test_explicit_level_wins_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(LEVEL_ENV_VAR, "ERROR")

    assert resolve_level("DEBUG") == logging.DEBUG
