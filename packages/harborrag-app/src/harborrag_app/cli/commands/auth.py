"""Operator authentication administration."""

from __future__ import annotations

from harborrag_app.cli.typer_app import new_typer

from .auth_keys import app as keys_app

app = new_typer(no_args_is_help=True, help="Manage credentials issued by this deployment.")
app.add_typer(keys_app, name="keys")
