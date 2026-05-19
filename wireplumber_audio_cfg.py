#!/usr/bin/env python3
"""
WirePlumber Audio Configurator
──────────────────────────────
GTK4 / libadwaita GUI with two tabs:
  • Configuration – Set sample rate & format per output device
  • Live Monitor  – Real-time display of the negotiated format and
                    all active playback streams

Dependencies (Ubuntu):
  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pipewire-bin

Run:
  python3 wireplumber_audio_cfg.py
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

import json
import os
import re
import subprocess
import threading
from datetime import datetime

# ── Flatpak sandbox detection ──────────────────────────────────────────────────
# When running inside Flatpak, host commands (pw-dump, wpctl, etc.) must be
# forwarded via flatpak-spawn --host, as they are absent from the sandbox.

IN_FLATPAK = os.path.exists("/.flatpak-info")


def _run(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper: forwards commands via flatpak-spawn --host when sandboxed."""
    if IN_FLATPAK:
        cmd = ["flatpak-spawn", "--host"] + cmd
    return subprocess.run(cmd, **kwargs)


# ── Constants ──────────────────────────────────────────────────────────────────

# None represents "Auto" – no entry written to the WirePlumber config
SAMPLE_RATES = [None,  44100, 48000, 88200, 96000, 176400, 192000]
RATE_LABELS  = ["Auto", "44.1 kHz", "48 kHz", "88.2 kHz", "96 kHz", "176.4 kHz", "192 kHz"]

FORMAT_KEYS   = [None,    "S16LE", "S24LE", "S24_3LE", "S32LE", "F32LE"]
FORMAT_LABELS = [
    "Auto",
    "S16LE  – 16-bit Integer",
    "S24LE  – 24-bit Integer (padded, 4 bytes)",
    "S24_3LE – 24-bit Integer (packed, 3 bytes)",
    "S32LE  – 32-bit Integer",
    "F32LE  – 32-bit Float",
]

CONFIG_DIR  = os.path.expanduser("~/.config/wireplumber/wireplumber.conf.d")
CONFIG_FILE = os.path.join(CONFIG_DIR, "51-audio-sample-rates.conf")

MONITOR_INTERVAL_MS = 1000


# ── PipeWire helpers ───────────────────────────────────────────────────────────

def get_audio_sinks() -> list:
    """Return all ALSA audio output devices via pw-dump."""
    try:
        result = _run(["pw-dump"], capture_output=True, text=True, timeout=10)
        nodes = json.loads(result.stdout)
    except FileNotFoundError:
        raise RuntimeError("pw-dump not found. Is pipewire-bin installed?")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse pw-dump output: {e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("pw-dump timed out.")

    sinks = []
    seen  = set()
    for node in nodes:
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        name = props.get("node.name", "")
        if not name.startswith("alsa_output") or name in seen:
            continue
        seen.add(name)
        sinks.append({
            "name":        name,
            "description": props.get("node.description") or name,
        })
    return sinks


def _parse_format_params(params: dict) -> dict:
    fmt_list = params.get("Format", [])
    if not fmt_list:
        return {}
    fmt = fmt_list[0]
    return {
        "rate":     fmt.get("rate"),
        "format":   fmt.get("format"),
        "channels": fmt.get("channels"),
    }


def _default_sink_from_metadata(nodes: list) -> str:
    """
    Read the default sink name from the PipeWire metadata node.
    Checks both common keys and handles values as dict or JSON string.
    No type-field check, as the value varies across PipeWire versions.
    """
    KEYS = ("default.audio.sink", "default.configured.audio.sink")

    for node in nodes:
        info  = node.get("info", {})
        props = info.get("props", {})
        if props.get("metadata.name") != "default":
            continue
        for entry in info.get("metadata", []):
            if entry.get("key") not in KEYS:
                continue
            value = entry.get("value", "")
            # Value may be a dict, JSON string, or plain string
            if isinstance(value, str) and value.startswith("{"):
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    pass
            if isinstance(value, dict):
                name = value.get("name", "")
            else:
                name = str(value)
            if name:
                return name
    return ""


def _default_sink_from_wpctl() -> str:
    """Fallback: read the default sink name via wpctl status + inspect."""
    try:
        result = _run(
            ["wpctl", "status"], capture_output=True, text=True, timeout=5
        )
        in_sinks = False
        for line in result.stdout.splitlines():
            if "Sinks:" in line:
                in_sinks = True
                continue
            if in_sinks:
                if line and not line.startswith(" ") and not line.startswith("\t") and ":" in line:
                    break
                if "*" in line:
                    m = re.search(r"\*\s+(\d+)\.", line)
                    if m:
                        node_id = m.group(1)
                        info = _run(
                            ["wpctl", "inspect", node_id],
                            capture_output=True, text=True, timeout=5
                        )
                        for iline in info.stdout.splitlines():
                            if "node.name" in iline:
                                nm = re.search(r'"([^"]+)"', iline)
                                if nm:
                                    return nm.group(1)
    except Exception:
        pass
    return ""


def get_monitor_data() -> dict:
    try:
        dump  = _run(["pw-dump"], capture_output=True, text=True, timeout=10)
        nodes = json.loads(dump.stdout)
    except FileNotFoundError as e:
        return {"error": f"pw-dump not found: {e}"}
    except Exception as e:
        return {"error": str(e)}

    # Default sink name: try metadata first, then wpctl as fallback
    default_sink = _default_sink_from_metadata(nodes)
    if not default_sink:
        default_sink = _default_sink_from_wpctl()

    sink_info = None
    streams   = []

    for node in nodes:
        info   = node.get("info", {})
        props  = info.get("props", {})
        params = info.get("params", {})
        cls    = props.get("media.class", "")
        name   = props.get("node.name", "")

        # Active output device
        if cls == "Audio/Sink":
            is_default = (name == default_sink) if default_sink else False
            fmt = _parse_format_params(params)
            if is_default or (sink_info is None and fmt.get("rate")):
                sink_info = {
                    "name":      props.get("node.description") or name,
                    "node_name": name,
                    "rate":      fmt.get("rate"),
                    "format":    fmt.get("format"),
                    "channels":  fmt.get("channels"),
                }
                if is_default:
                    default_sink = "__found__"

        # Active playback streams
        elif cls == "Stream/Output/Audio":
            fmt      = _parse_format_params(params)
            app_name = (
                props.get("application.name")
                or props.get("media.name")
                or props.get("node.name")
                or "Unknown"
            )
            if fmt.get("rate"):
                streams.append({
                    "app":      app_name,
                    "rate":     fmt.get("rate"),
                    "format":   fmt.get("format"),
                    "channels": fmt.get("channels"),
                })

    return {
        "sink":    sink_info,
        "streams": streams,
    }


# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config = {}
    if not os.path.exists(CONFIG_FILE):
        return config
    try:
        content = open(CONFIG_FILE).read()
        for block in re.split(r'\}\s*\{', content):
            name_m   = re.search(r'node\.name\s*=\s*"([^"]+)"', block)
            rate_m   = re.search(r'audio\.rate\s*=\s*(\d+)',     block)
            format_m = re.search(r'audio\.format\s*=\s*"([^"]+)"', block)
            if name_m and (rate_m or format_m):
                config[name_m.group(1)] = {
                    "rate":   int(rate_m.group(1)) if rate_m   else None,
                    "format": format_m.group(1)    if format_m else None,
                }
    except Exception as e:
        print(f"[Warning] Config read error: {e}")
    return config


def write_config(device_settings: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)

    active = {
        name: s for name, s in device_settings.items()
        if s.get("rate") is not None or s.get("format") is not None
    }

    if not active:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        return

    lines = [
        "# Auto-generated by WirePlumber Audio Configurator",
        "# Manual changes will be overwritten on next save.",
        "",
        "monitor.alsa.rules = [",
    ]
    for name, s in active.items():
        lines += [
            "  {",
            "    matches = [",
            "      {",
            f'        node.name = "{name}"',
            "      }",
            "    ]",
            "    actions = {",
            "      update-props = {",
        ]
        if s.get("format") is not None:
            lines.append(f'        audio.format = "{s["format"]}"')
        if s.get("rate") is not None:
            lines.append(f'        audio.rate   = {s["rate"]}')
        lines += [
            "      }",
            "    }",
            "  }",
        ]
    lines.append("]")

    with open(CONFIG_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


# ── Value label helper ─────────────────────────────────────────────────────────

def _value_label(text: str = "—") -> Gtk.Label:
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lbl.add_css_class("monospace")
    lbl.add_css_class("dim-label")
    lbl.set_xalign(1.0)
    return lbl


def _fmt_rate(rate) -> str:
    if rate is None:
        return "—"
    khz = rate / 1000
    return f"{khz:g} kHz  ({rate} Hz)"


def _fmt_channels(ch) -> str:
    if ch is None:
        return "—"
    names = {1: "Mono", 2: "Stereo", 4: "Quadro", 6: "5.1", 8: "7.1"}
    return f"{names.get(ch, str(ch))}  ({ch} ch)"


# ── Tab 1: Configuration ───────────────────────────────────────────────────────

class DeviceRow(Adw.ActionRow):
    def __init__(self, device: dict, saved: dict):
        super().__init__()
        self.device = device
        self.set_title(device["description"])
        self.set_subtitle(device["name"])

        self.rate_dd = Gtk.DropDown(
            model=Gtk.StringList.new(RATE_LABELS),
            valign=Gtk.Align.CENTER,
        )
        saved_rate = saved.get("rate")
        self.rate_dd.set_selected(
            SAMPLE_RATES.index(saved_rate) if saved_rate in SAMPLE_RATES else 0
        )

        self.fmt_dd = Gtk.DropDown(
            model=Gtk.StringList.new(FORMAT_LABELS),
            valign=Gtk.Align.CENTER,
        )
        saved_fmt = saved.get("format")
        self.fmt_dd.set_selected(
            FORMAT_KEYS.index(saved_fmt) if saved_fmt in FORMAT_KEYS else 0
        )

        box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        box.append(self.rate_dd)
        box.append(self.fmt_dd)
        self.add_suffix(box)

    def get_settings(self) -> dict:
        return {
            "rate":   SAMPLE_RATES[self.rate_dd.get_selected()],
            "format": FORMAT_KEYS[self.fmt_dd.get_selected()],
        }


class ConfigPage(Gtk.Box):
    def __init__(self, toast_overlay):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._overlay = toast_overlay
        self._rows = []

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        clamp  = Adw.Clamp(maximum_size=820, tightening_threshold=640)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_top=20, margin_bottom=20,
            margin_start=20, margin_end=20,
        )

        self.banner = Adw.Banner(
            title="No ALSA output devices found – is PipeWire running?",
            button_label="Try Again",
        )
        self.banner.connect("button-clicked", lambda _: self.load_devices())
        outer.append(self.banner)

        self.spinner_row = Adw.ActionRow(title="Loading devices…")
        self.spinner_row.add_suffix(
            Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER)
        )

        self.group = Adw.PreferencesGroup(
            title="Output Devices",
            description="Configure sample rate and bit depth per device",
        )
        outer.append(self.group)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(sep)

        self.apply_btn = Gtk.Button(label="Apply & Restart WirePlumber")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.add_css_class("pill")
        self.apply_btn.set_halign(Gtk.Align.END)
        self.apply_btn.connect("clicked", self._on_apply)
        outer.append(self.apply_btn)

        path_label = Gtk.Label(
            label=f"<small>Config file: <tt>{CONFIG_FILE}</tt></small>",
            use_markup=True,
            halign=Gtk.Align.START,
            wrap=True,
        )
        path_label.add_css_class("dim-label")
        outer.append(path_label)

        clamp.set_child(outer)
        scroll.set_child(clamp)
        self.append(scroll)

        self.load_devices()

    def load_devices(self):
        for row in self._rows:
            self.group.remove(row)
        self._rows.clear()
        self.group.add(self.spinner_row)
        self.apply_btn.set_sensitive(False)

        def worker():
            try:
                sinks    = get_audio_sinks()
                existing = load_config()
                GLib.idle_add(self._populate, sinks, existing, None)
            except Exception as e:
                GLib.idle_add(self._populate, [], {}, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, sinks, existing, error):
        self.group.remove(self.spinner_row)
        self.apply_btn.set_sensitive(True)
        if error:
            self.banner.set_title(f"Error: {error}")
            self.banner.set_revealed(True)
            return
        self.banner.set_revealed(not sinks)
        for sink in sinks:
            row = DeviceRow(sink, existing.get(sink["name"], {}))
            self.group.add(row)
            self._rows.append(row)

    def _on_apply(self, _):
        settings = {r.device["name"]: r.get_settings() for r in self._rows}
        try:
            write_config(settings)
        except Exception as e:
            self._toast(f"Failed to write config: {e}", is_error=True)
            return

        self.apply_btn.set_sensitive(False)
        self.apply_btn.set_label("Restarting…")

        def restart():
            try:
                _run(
                    ["systemctl", "--user", "restart", "wireplumber"],
                    check=True, timeout=15,
                )
                GLib.idle_add(self._after_restart, None)
            except subprocess.CalledProcessError as e:
                GLib.idle_add(self._after_restart, str(e))
            except subprocess.TimeoutExpired:
                GLib.idle_add(self._after_restart, "Restart timed out.")

        threading.Thread(target=restart, daemon=True).start()

    def _after_restart(self, error):
        self.apply_btn.set_sensitive(True)
        self.apply_btn.set_label("Apply & Restart WirePlumber")
        if error:
            self._toast(f"WirePlumber restart failed: {error}", is_error=True)
        else:
            self._toast("✓ Saved – WirePlumber restarted.")

    def _toast(self, msg: str, is_error: bool = False):
        toast = Adw.Toast(title=msg, timeout=0 if is_error else 4)
        self._overlay.add_toast(toast)


# ── Tab 2: Live Monitor ────────────────────────────────────────────────────────

class StreamRow(Adw.ActionRow):
    def __init__(self, stream: dict):
        super().__init__()
        self.set_title(stream["app"])

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        box.append(_value_label(_fmt_rate(stream.get("rate"))))
        box.append(_value_label(stream.get("format") or "—"))
        box.append(_value_label(_fmt_channels(stream.get("channels"))))
        self.add_suffix(box)


class MonitorPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._active      = False
        self._stream_rows = []

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        clamp  = Adw.Clamp(maximum_size=820, tightening_threshold=640)

        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=20,
            margin_top=20, margin_bottom=20,
            margin_start=20, margin_end=20,
        )

        # Active output device group
        sink_group = Adw.PreferencesGroup(title="Active Output Device")
        outer.append(sink_group)

        self._sink_name_row = self._make_info_row("Device")
        self._sink_rate_row = self._make_info_row("Sample Rate (negotiated)")
        self._sink_fmt_row  = self._make_info_row("Format / Bit Depth")
        self._sink_ch_row   = self._make_info_row("Channels")
        for row in (self._sink_name_row, self._sink_rate_row,
                    self._sink_fmt_row,  self._sink_ch_row):
            sink_group.add(row)

        # Active playback streams group
        self._streams_group = Adw.PreferencesGroup(
            title="Active Playback Streams (Input Signal)",
            description=(
                "Format of audio sources currently being sent to the output device.\n"
                "Each row: Sample Rate · Format · Channels"
            ),
        )
        outer.append(self._streams_group)

        self._no_streams_row = Adw.ActionRow(title="No active streams")
        self._no_streams_row.add_suffix(Gtk.Label(label="—", valign=Gtk.Align.CENTER))
        self._streams_group.add(self._no_streams_row)

        # Timestamp label
        self._ts_label = Gtk.Label(halign=Gtk.Align.END, use_markup=True)
        self._ts_label.add_css_class("dim-label")
        outer.append(self._ts_label)
        self._set_timestamp()

        clamp.set_child(outer)
        scroll.set_child(clamp)
        self.append(scroll)

    @staticmethod
    def _make_info_row(title: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        lbl = _value_label()
        row.add_suffix(lbl)
        row._val = lbl
        return row

    @staticmethod
    def _set_val(row, text: str):
        row._val.set_label(text)

    def _set_timestamp(self):
        now = datetime.now().strftime("%H:%M:%S")
        self._ts_label.set_label(f"<small>Last updated: {now}</small>")

    def start_polling(self):
        if self._active:
            return
        self._active = True
        self._do_poll()

    def stop_polling(self):
        self._active = False

    def _do_poll(self):
        if not self._active:
            return
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self):
        data = get_monitor_data()
        GLib.idle_add(self._update_ui, data)

    def _update_ui(self, data: dict):
        if not self._active:
            return

        sink = data.get("sink")
        if data.get("error"):
            self._set_val(self._sink_name_row, f"Error: {data['error']}")
            for row in (self._sink_rate_row, self._sink_fmt_row, self._sink_ch_row):
                self._set_val(row, "—")
        elif sink:
            self._set_val(self._sink_name_row, sink["name"])
            self._set_val(self._sink_rate_row, _fmt_rate(sink.get("rate")))
            self._set_val(self._sink_fmt_row,  sink.get("format") or "—")
            self._set_val(self._sink_ch_row,   _fmt_channels(sink.get("channels")))
        else:
            self._set_val(self._sink_name_row, "No default device found")
            for row in (self._sink_rate_row, self._sink_fmt_row, self._sink_ch_row):
                self._set_val(row, "—")

        for row in self._stream_rows:
            self._streams_group.remove(row)
        self._stream_rows.clear()

        streams = data.get("streams", [])
        self._no_streams_row.set_visible(not streams)

        for s in streams:
            row = StreamRow(s)
            self._streams_group.add(row)
            self._stream_rows.append(row)

        self._set_timestamp()
        GLib.timeout_add(MONITOR_INTERVAL_MS, self._do_poll)


# ── Main window ────────────────────────────────────────────────────────────────

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("WirePlumber Audio Configurator")
        self.set_default_size(780, 560)

        overlay = Adw.ToastOverlay()
        tv      = Adw.ToolbarView()
        hb      = Adw.HeaderBar()

        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text("Reload device list")
        hb.pack_start(self._refresh_btn)

        self._stack   = Adw.ViewStack()
        switcher      = Adw.ViewSwitcherTitle(stack=self._stack)
        hb.set_title_widget(switcher)

        switcher_bar = Adw.ViewSwitcherBar(stack=self._stack)
        switcher.bind_property("title-visible", switcher_bar, "reveal")
        tv.add_bottom_bar(switcher_bar)

        tv.add_top_bar(hb)

        self._config_page = ConfigPage(toast_overlay=overlay)
        self._stack.add_titled_with_icon(
            self._config_page, "config",
            "Configuration", "preferences-system-symbolic",
        )

        self._monitor_page = MonitorPage()
        self._stack.add_titled_with_icon(
            self._monitor_page, "monitor",
            "Live Monitor", "utilities-system-monitor-symbolic",
        )

        self._refresh_btn.connect(
            "clicked", lambda _: self._config_page.load_devices()
        )
        self._stack.connect("notify::visible-child", self._on_page_changed)

        tv.set_content(self._stack)
        overlay.set_child(tv)
        self.set_content(overlay)

    def _on_page_changed(self, stack, _param):
        is_monitor = (stack.get_visible_child() is self._monitor_page)
        self._refresh_btn.set_visible(not is_monitor)
        if is_monitor:
            self._monitor_page.start_polling()
        else:
            self._monitor_page.stop_polling()


# ── Entry point ────────────────────────────────────────────────────────────────

class AudioCfgApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.wireplumber_audio_cfg")
        self.connect("activate", self._on_activate)

    def _on_activate(self, _):
        win = MainWindow(application=self)
        win.present()


if __name__ == "__main__":
    AudioCfgApp().run()
