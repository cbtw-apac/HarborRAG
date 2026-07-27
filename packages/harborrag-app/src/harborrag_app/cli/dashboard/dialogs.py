from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

type BindingSpec = Binding | tuple[str, str] | tuple[str, str, str]


class CancelConfirmation(ModalScreen[bool]):
    """Confirm a destructive dashboard cancellation action."""

    BINDINGS: ClassVar[list[BindingSpec]] = [
        Binding("escape", "dismiss(False)", "Keep running", show=False),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        with Container(id="cancel-dialog"):
            yield Label("Cancel ingestion?", id="cancel-title")
            yield Static(
                Text.assemble(
                    "Request graceful cancellation for ",
                    (self.run_id, "bold cyan"),
                    "? Reconciliation will still run.",
                ),
                id="cancel-copy",
            )
            with Horizontal(id="cancel-actions"):
                yield Button("Keep running", id="keep", variant="default")
                yield Button("Cancel run", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")
