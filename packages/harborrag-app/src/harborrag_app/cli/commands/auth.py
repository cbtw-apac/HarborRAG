"""Operator authentication administration."""

from __future__ import annotations

import typer

from .auth_keys import app as keys_app

app = typer.Typer(no_args_is_help=True, help="Manage credentials issued by this deployment.")
app.add_typer(keys_app, name="keys")
