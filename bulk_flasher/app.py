"""Textual TUI for the Fri3d bulk flasher."""

from __future__ import annotations

import sys
import threading

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from . import firmware
from .devices import DEVICES, Device, device_by_id
from .flasher import Stats, bulk_flash_loop


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
    #stats {
        margin-top: 1;
        text-style: bold;
    }
    Button {
        width: 100%;
        margin-bottom: 1;
    }
    #log {
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("f", "fetch", "Fetch firmware"),
        ("s", "start_stop", "Start/Stop"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.selected_device: Device = DEVICES[0]
        self.cancel_event: threading.Event | None = None
        self.stats = Stats()
        self.busy = False  # a fetch or flash worker is running

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
                yield Button("Fetch latest firmware [f]", id="fetch")
                yield Button("Start bulk flashing [s]", id="start", variant="success")
                yield Button("Stop [s]", id="stop", variant="error", disabled=True)
                yield Static("", id="stats")
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#devices", OptionList).highlighted = 0
        self._refresh_fw_info()
        self._refresh_stats()
        self.log_line("Select a device, fetch firmware, then start bulk flashing.")

    # ------------------------------------------------------------ helpers --

    def log_line(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def _log_from_thread(self, line: str) -> None:
        self.call_from_thread(self.log_line, line)

    def _refresh_fw_info(self) -> None:
        info = firmware.get_cached(self.selected_device)
        widget = self.query_one("#fw-info", Static)
        if info:
            size_mb = info.size / (1024 * 1024)
            widget.update(f"{info.tag}\n{info.asset_name} ({size_mb:.1f} MB)")
        else:
            widget.update("Not downloaded yet.\nPress [f] to fetch.")

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

    # ------------------------------------------------------------- events --

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_id and not self.busy:
            self.selected_device = device_by_id(event.option_id)
            self._refresh_fw_info()

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
        device = self.selected_device
        self.run_worker(
            lambda: self._fetch_worker(device), thread=True, exclusive=False
        )

    def _fetch_worker(self, device: Device) -> None:
        try:
            firmware.fetch_latest(device, self._log_from_thread)
        except Exception as exc:  # noqa: BLE001 — surface any failure in the log
            self._log_from_thread(f"ERROR fetching firmware: {exc}")
        finally:
            self.call_from_thread(self._fetch_done)

    def _fetch_done(self) -> None:
        self.busy = False
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
            self.run_worker(
                lambda: self._fetch_then_start(device), thread=True, exclusive=False
            )
            return
        self._launch_flash_loop(device, info)

    def _fetch_then_start(self, device: Device) -> None:
        try:
            info = firmware.fetch_latest(device, self._log_from_thread)
        except Exception as exc:  # noqa: BLE001
            self._log_from_thread(f"ERROR fetching firmware: {exc}")
            self.call_from_thread(self._fetch_done)
            return
        self.call_from_thread(self._fetch_done)
        self.call_from_thread(self._launch_flash_loop, device, info)

    def _launch_flash_loop(self, device: Device, info: firmware.FirmwareInfo) -> None:
        self.busy = True
        self.cancel_event = threading.Event()
        self.stats = Stats()
        self._refresh_stats()
        self._set_flashing_ui(True)
        cancel = self.cancel_event
        stats = self.stats
        self.run_worker(
            lambda: self._flash_worker(device, info, cancel, stats),
            thread=True,
            exclusive=False,
        )

    def _flash_worker(
        self,
        device: Device,
        info: firmware.FirmwareInfo,
        cancel: threading.Event,
        stats: Stats,
    ) -> None:
        try:
            bulk_flash_loop(
                device,
                info.path,
                cancel,
                self._log_from_thread,
                stats,
                lambda: self.call_from_thread(self._refresh_stats),
            )
        except Exception as exc:  # noqa: BLE001
            self._log_from_thread(f"ERROR: {exc}")
        finally:
            self.call_from_thread(self._flash_done)

    def _flash_done(self) -> None:
        self.busy = False
        self.cancel_event = None
        self._set_flashing_ui(False)

    def _stop_flashing(self) -> None:
        if self.cancel_event is not None:
            self.log_line("Stopping after current operation...")
            self.cancel_event.set()
            self.query_one("#stop", Button).disabled = True


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
