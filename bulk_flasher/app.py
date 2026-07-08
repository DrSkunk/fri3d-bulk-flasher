"""Textual TUI for the Fri3d bulk flasher."""

from __future__ import annotations

import dataclasses
import sys
import threading

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import firmware
from .config import CONFIG_PATH, Config, load_config
from .devices import DEVICES, Device, device_by_id
from .flasher import Slot, Stats, parallel_flash_loop, wchisp_flash_loop

# Minimum card width (incl. border/gutter) used to pick the slot grid columns.
SLOT_CARD_WIDTH = 34
PROGRESS_BAR_WIDTH = 24


class SlotWidget(Static):
    """One flashing-slot card in the status grid."""

    def __init__(self, index: int) -> None:
        super().__init__(classes="slot slot-waiting")
        self.index = index
        self.show_waiting()

    def show_waiting(self) -> None:
        self.set_classes("slot slot-waiting")
        self.update(
            f"[dim]Slot {self.index + 1}[/dim]\n"
            "[dim]— waiting for device —[/dim]"
        )

    def show(self, snap: dict) -> None:
        state = snap["state"]
        if state == "waiting":
            self.show_waiting()
            return
        self.set_classes(f"slot slot-{state}")
        port = snap["port"].removeprefix("/dev/")
        title = f"[b]#{snap['unit']} {escape(port)}[/b]"
        detail = escape(snap["detail"][:60])
        if state == "flashing":
            progress = max(0, snap["progress"])
            filled = progress * PROGRESS_BAR_WIDTH // 100
            bar = "█" * filled + "─" * (PROGRESS_BAR_WIDTH - filled)
            self.update(f"{title}\n{bar} {progress:3d}%\n[dim]{detail}[/dim]")
        elif state == "success":
            self.update(f"{title}\n[green]✔ Flashed OK[/green]\n[dim]{detail}[/dim]")
        else:  # failed
            self.update(f"{title}\n[red]✖ FAILED[/red]\n[dim]{detail}[/dim]")


class BulkFlasherApp(App):
    TITLE = "Fri3d Bulk Flasher"
    SUB_TITLE = "Badge / Communicator / DJ Addon 2026"

    CSS = """
    #sidebar {
        width: 44;
        padding: 1;
        border-right: solid $primary;
    }
    #devices {
        height: 5;
        margin-bottom: 1;
    }
    .section-title {
        text-style: bold;
        color: $accent;
    }
    #fw-info {
        margin-bottom: 1;
        color: $text-muted;
        min-height: 3;
    }
    #parallel-info {
        margin-bottom: 1;
        color: $text-muted;
    }
    #stats {
        margin-top: 1;
        text-style: bold;
    }
    Button {
        width: 100%;
        margin-bottom: 1;
    }
    #slots {
        height: auto;
        grid-gutter: 0 1;
        padding: 0 1;
    }
    .slot {
        height: 5;
        padding: 0 1;
        border: round $panel-lighten-2;
    }
    .slot-flashing {
        border: round $warning;
    }
    .slot-success {
        border: round $success;
    }
    .slot-failed {
        border: round $error;
    }
    #log {
        padding: 0 1;
        min-height: 5;
    }
    """

    BINDINGS = [
        ("f", "fetch", "Fetch firmware"),
        ("s", "start_stop", "Start/Stop"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config_data: Config = load_config()
        self.selected_device: Device = DEVICES[0]
        self.cancel_event: threading.Event | None = None
        self.fetch_cancel: threading.Event | None = None
        self.stats = Stats()
        self.busy = False  # a fetch or flash worker is running
        self.slots: list[Slot] = []
        self.slot_widgets: list[SlotWidget] = []

    # ------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("Device", classes="section-title")
                yield OptionList(
                    *[Option(d.name, id=d.id) for d in DEVICES], id="devices"
                )
                yield Static("Firmware", classes="section-title")
                yield Static("", id="fw-info")
                yield Static("", id="parallel-info")
                yield Button("Fetch latest firmware [f]", id="fetch")
                yield Button("Start bulk flashing [s]", id="start", variant="success")
                yield Button("Stop [s]", id="stop", variant="error", disabled=True)
                yield Static("", id="stats")
            with Vertical(id="main"):
                yield Grid(id="slots")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#devices", OptionList).highlighted = 0
        self._refresh_fw_info()
        self._refresh_parallel_info()
        self._refresh_stats()
        self._rebuild_slot_widgets(self._slot_count(self.selected_device))
        self.log_line("Select a device, fetch firmware, then start bulk flashing.")

    def on_resize(self, event) -> None:
        self._layout_slots()

    # ------------------------------------------------------------ helpers --

    def log_line(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def _call_ui(self, fn, *args) -> None:
        """call_from_thread that tolerates the app already shutting down."""
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            pass

    def _log_from_thread(self, line: str) -> None:
        self._call_ui(self.log_line, line)

    def _slot_count(self, device: Device) -> int:
        # wchisp can only talk to one bootloader device at a time.
        return self.config_data.max_parallel if device.method == "esptool" else 1

    def _refresh_fw_info(self) -> None:
        info = firmware.get_cached(self.selected_device)
        widget = self.query_one("#fw-info", Static)
        if info:
            size = firmware.format_size(info.size)
            widget.update(f"{info.tag}\n{info.asset_name} ({size})")
        else:
            widget.update("Not downloaded yet.\nPress [f] to fetch.")

    def _refresh_parallel_info(self) -> None:
        widget = self.query_one("#parallel-info", Static)
        if self.selected_device.method == "esptool":
            baud = self.config_data.baud or self.selected_device.baud
            widget.update(
                f"Parallel: up to {self.config_data.max_parallel} at once\n"
                f"Baud: {baud}\n"
                f"(configure in {CONFIG_PATH})"
            )
        else:
            widget.update("Sequential: one device at a time\n(wchisp limitation)")

    def _refresh_stats(self) -> None:
        with self.stats.lock:
            text = (
                f"OK: {self.stats.succeeded}   "
                f"Failed: {self.stats.failed}   "
                f"Total: {self.stats.attempted}"
            )
        self.query_one("#stats", Static).update(text)

    def _set_flashing_ui(self, flashing: bool) -> None:
        self.query_one("#start", Button).disabled = flashing
        self.query_one("#fetch", Button).disabled = flashing
        self.query_one("#stop", Button).disabled = not flashing
        self.query_one("#devices", OptionList).disabled = flashing

    # --------------------------------------------------------- slot grid --

    def _rebuild_slot_widgets(self, count: int) -> None:
        grid = self.query_one("#slots", Grid)
        grid.remove_children()
        self.slot_widgets = [SlotWidget(i) for i in range(count)]
        grid.mount(*self.slot_widgets)
        self._layout_slots()

    def _layout_slots(self) -> None:
        grid = self.query_one("#slots", Grid)
        count = len(self.slot_widgets)
        if not count:
            return
        width = grid.size.width or grid.container_size.width
        columns = max(1, min(count, width // SLOT_CARD_WIDTH)) if width else 1
        grid.styles.grid_size_columns = columns

    def _update_slot(self, slot: Slot) -> None:
        if slot.index < len(self.slot_widgets):
            self.slot_widgets[slot.index].show(slot.snapshot())

    def _slot_changed_from_thread(self, slot: Slot) -> None:
        self._call_ui(self._update_slot, slot)

    # ------------------------------------------------------------- events --

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_id and not self.busy:
            self.selected_device = device_by_id(event.option_id)
            self._refresh_fw_info()
            self._refresh_parallel_info()
            self._rebuild_slot_widgets(self._slot_count(self.selected_device))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fetch":
            self.action_fetch()
        elif event.button.id == "start":
            self._start_flashing()
        elif event.button.id == "stop":
            self._stop_flashing()

    # ------------------------------------------------------------ actions --

    def action_fetch(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.query_one("#fetch", Button).disabled = True
        self.query_one("#start", Button).disabled = True
        self.fetch_cancel = threading.Event()
        device = self.selected_device
        self.run_worker(
            lambda: self._fetch_worker(device), thread=True, exclusive=False
        )

    def _fetch_worker(self, device: Device) -> None:
        try:
            firmware.fetch_latest(device, self._log_from_thread, self.fetch_cancel)
        except firmware.FetchCancelled:
            self._log_from_thread("Fetch cancelled.")
        except Exception as exc:  # noqa: BLE001 — surface any failure in the log
            self._log_from_thread(f"ERROR fetching firmware: {exc}")
        finally:
            self._call_ui(self._fetch_done)

    def _fetch_done(self) -> None:
        self.busy = False
        self.fetch_cancel = None
        self.query_one("#fetch", Button).disabled = False
        self.query_one("#start", Button).disabled = False
        self._refresh_fw_info()

    def action_start_stop(self) -> None:
        if self.cancel_event is not None:
            self._stop_flashing()
        elif not self.busy:
            self._start_flashing()

    def _start_flashing(self) -> None:
        if self.busy:
            return
        device = self.selected_device
        info = firmware.get_cached(device)
        if info is None:
            self.log_line("No firmware downloaded yet — fetching latest first...")
            self.busy = True
            self.query_one("#fetch", Button).disabled = True
            self.query_one("#start", Button).disabled = True
            self.fetch_cancel = threading.Event()
            self.run_worker(
                lambda: self._fetch_then_start(device), thread=True, exclusive=False
            )
            return
        self._launch_flash_loop(device, info)

    def _fetch_then_start(self, device: Device) -> None:
        try:
            info = firmware.fetch_latest(
                device, self._log_from_thread, self.fetch_cancel
            )
        except firmware.FetchCancelled:
            self._log_from_thread("Fetch cancelled.")
            self._call_ui(self._fetch_done)
            return
        except Exception as exc:  # noqa: BLE001
            self._log_from_thread(f"ERROR fetching firmware: {exc}")
            self._call_ui(self._fetch_done)
            return
        self._call_ui(self._fetch_done)
        self._call_ui(self._launch_flash_loop, device, info)

    def _launch_flash_loop(self, device: Device, info: firmware.FirmwareInfo) -> None:
        if device.method == "esptool" and self.config_data.baud:
            device = dataclasses.replace(device, baud=self.config_data.baud)
        self.busy = True
        self.cancel_event = threading.Event()
        self.stats = Stats()
        self._refresh_stats()
        self._set_flashing_ui(True)
        self.slots = [Slot(i) for i in range(self._slot_count(device))]
        self._rebuild_slot_widgets(len(self.slots))
        cancel = self.cancel_event
        stats = self.stats
        slots = self.slots
        self.run_worker(
            lambda: self._flash_worker(device, info, cancel, stats, slots),
            thread=True,
            exclusive=False,
        )

    def _flash_worker(
        self,
        device: Device,
        info: firmware.FirmwareInfo,
        cancel: threading.Event,
        stats: Stats,
        slots: list[Slot],
    ) -> None:
        try:
            if device.method == "esptool":
                parallel_flash_loop(
                    device,
                    info.path,
                    cancel,
                    self._log_from_thread,
                    stats,
                    lambda: self._call_ui(self._refresh_stats),
                    slots,
                    self._slot_changed_from_thread,
                )
            else:
                wchisp_flash_loop(
                    device,
                    info.path,
                    cancel,
                    self._log_from_thread,
                    stats,
                    lambda: self._call_ui(self._refresh_stats),
                    slots[0],
                    self._slot_changed_from_thread,
                )
        except Exception as exc:  # noqa: BLE001
            self._log_from_thread(f"ERROR: {exc}")
        finally:
            self._call_ui(self._flash_done)

    def _flash_done(self) -> None:
        self.busy = False
        self.cancel_event = None
        self._set_flashing_ui(False)

    def _stop_flashing(self) -> None:
        if self.cancel_event is not None:
            self.log_line("Stopping after current operation...")
            self.cancel_event.set()
            self.query_one("#stop", Button).disabled = True

    async def action_quit(self) -> None:
        # Signal the flash worker thread before exiting: the executor thread
        # is non-daemon, so leaving it running keeps the process alive after
        # the UI has closed.
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.fetch_cancel is not None:
            self.fetch_cancel.set()
        self.exit()


def main() -> None:
    # Frozen-binary dispatch: the flasher re-invokes this executable as
    # `<exe> --esptool <args>` because a PyInstaller bundle has no
    # interpreter to run `python -m esptool`.
    if len(sys.argv) > 1 and sys.argv[1] == "--esptool":
        sys.argv = ["esptool"] + sys.argv[2:]
        from esptool import _main as esptool_main

        esptool_main()
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        from . import __version__

        print(f"fri3d-bulk-flasher {__version__}")
        return
    BulkFlasherApp().run()


if __name__ == "__main__":
    main()
