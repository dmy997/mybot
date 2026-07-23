"""Modal dialog screens for the mybot TUI."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView


class ConfirmScreen(ModalScreen[bool]):
    """Confirmation dialog for destructive actions (/exit, /clear)."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }

    ConfirmScreen #dialog {
        width: 40;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    ConfirmScreen #dialog Label {
        text-align: center;
        margin-bottom: 1;
    }

    ConfirmScreen #dialog Horizontal {
        align: center middle;
    }

    ConfirmScreen Button {
        margin: 1 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Yes", variant="primary", id="yes")
                yield Button("No", variant="error", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class SessionListScreen(ModalScreen[None]):
    """Display all available sessions."""

    DEFAULT_CSS = """
    SessionListScreen {
        align: center middle;
    }

    SessionListScreen #dialog {
        width: 50;
        height: 20;
        border: thick $primary;
        background: $surface;
        padding: 1;
    }

    SessionListScreen #dialog Label {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    SessionListScreen #sessions-list {
        height: 1fr;
        overflow-y: auto;
    }

    SessionListScreen Button {
        margin-top: 1;
    }
    """

    def __init__(self, sessions: list[dict]) -> None:
        super().__init__()
        self._sessions = sessions

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(f"Sessions ({len(self._sessions)})")
            with VerticalScroll(id="sessions-list"):
                yield ListView(id="session-list")
            yield Button("Close", variant="primary", id="close")

    def on_mount(self) -> None:
        lst = self.query_one("#session-list", ListView)
        for sess in self._sessions:
            key = sess.get("key", "?")
            updated = str(sess.get("updated_at", ""))[:16]
            lst.append(ListItem(Label(f"{key}  [{updated}]")))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class PlanApprovalScreen(ModalScreen[str]):
    """Plan review dialog for plan_solve and deep_research approval.

    Dismisses with ``"approved"``, ``"denied"``, or edited plan text.
    """

    DEFAULT_CSS = """
    PlanApprovalScreen {
        align: center middle;
    }
    PlanApprovalScreen #dialog {
        width: 60;
        height: 30;
        border: thick $secondary;
        background: $surface;
        padding: 1;
    }
    PlanApprovalScreen #plan-content {
        height: 1fr;
        overflow-y: auto;
        border: solid $panel;
        padding: 0 1;
        margin-bottom: 1;
    }
    PlanApprovalScreen #plan-edit {
        height: 1fr;
        margin-bottom: 1;
    }
    PlanApprovalScreen .hidden { display: none; }
    """

    def __init__(self, plan_type: str, plan_content: str) -> None:
        super().__init__()
        self._plan_type = plan_type
        self._plan_content = plan_content

    def compose(self) -> ComposeResult:
        from textual.widgets import TextArea
        label = "Plan-and-Solve" if self._plan_type == "plan_solve" else "Deep Research"
        with Container(id="dialog"):
            yield Label(f"[bold]Plan Review: {label}[/bold]")
            yield Label(self._plan_content[:2000], id="plan-content")
            yield TextArea(self._plan_content, id="plan-edit", classes="hidden")
            with Container(id="edit-buttons", classes="hidden"):
                yield Button("Submit Edit", variant="primary", id="btn-submit-edit")
            with Container(id="approve-buttons"):
                yield Button("Approve", variant="primary", id="btn-approve")
                yield Button("Edit", variant="warning", id="btn-edit")
                yield Button("Reject", variant="error", id="btn-reject")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-approve":
            self.dismiss("approved")
        elif bid == "btn-reject":
            self.dismiss("denied")
        elif bid == "btn-edit":
            self.query_one("#plan-content").add_class("hidden")
            self.query_one("#plan-edit").remove_class("hidden")
            self.query_one("#approve-buttons").add_class("hidden")
            self.query_one("#edit-buttons").remove_class("hidden")
        elif bid == "btn-submit-edit":
            from textual.widgets import TextArea
            text = self.query_one("#plan-edit", TextArea).text
            self.dismiss(text)
