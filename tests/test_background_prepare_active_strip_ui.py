from pathlib import Path
from types import SimpleNamespace

from dzll_launcher.background_prepare import (
    BackgroundPreparationRuntime,
    BackgroundServerPreparationSnapshot,
)
from dzll_launcher.background_prepare_queue import BackgroundPreparationQueue
from dzll_launcher.preparation_presentation import PreparationProgressMode
from dzll_launcher.ui_row import ServerObject
from dzll_launcher.window import DZLLWindow


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
STYLE_SOURCE = (ROOT / "src/dzll_launcher/styles.py").read_text(encoding="utf-8")
SIDEBAR_SOURCE = (ROOT / "src/dzll_launcher/sidebar_ui.py").read_text(encoding="utf-8")


class Widget:
    def __init__(self):
        self.text = ""
        self.visible = False
        self.sensitive = True
        self.fraction = 0.0
        self.tooltip = None
        self.css_classes = set()
        self.margin_start = 0
        self.margin_end = 0
        self.margin_bottom = 0
        self.size_request = None
        self.vexpand = False
        self.valign = None
        self.opacity = 1.0

    def set_text(self, value):
        self.text = str(value)

    def set_label(self, value):
        self.text = str(value)

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_fraction(self, value):
        self.fraction = float(value)

    def set_tooltip_text(self, value):
        self.tooltip = value

    def add_css_class(self, value):
        self.css_classes.add(value)

    def remove_css_class(self, value):
        self.css_classes.discard(value)

    def set_margin_start(self, value):
        self.margin_start = int(value)

    def set_margin_end(self, value):
        self.margin_end = int(value)

    def set_margin_bottom(self, value):
        self.margin_bottom = int(value)

    def set_size_request(self, width, height):
        self.size_request = (width, height)

    def set_vexpand(self, value):
        self.vexpand = bool(value)

    def set_valign(self, value):
        self.valign = value

    def set_opacity(self, value):
        self.opacity = float(value)


def _runtime():
    return BackgroundPreparationRuntime(
        "/workshop", "", "", False, False, True,
        "steam_client", True, False,
    )


def _server(ip, name):
    return ServerObject(
        ip=ip, gport=2302, qport=27016, name=name, mods_json="[]",
    )


def _snapshot(server):
    return BackgroundServerPreparationSnapshot(
        server.ip, server.gport, server.qport, server.name, ((101, "One"),),
    )


def _host(queue=None):
    host = SimpleNamespace(
        _background_prepare_queue=queue or BackgroundPreparationQueue(),
        _background_prepare_active=False,
        background_prepare_status=Widget(),
        background_prepare_info=Widget(),
        background_prepare_server_label=Widget(),
        background_prepare_detail_label=Widget(),
        background_prepare_queue_label=Widget(),
        background_prepare_queue_divider=Widget(),
        background_prepare_count_label=Widget(),
        background_prepare_retry_btn=Widget(),
        background_prepare_failed_label=Widget(),
        background_prepare_action_btn=Widget(),
        background_prepare_right_status_label=Widget(),
        background_prepare_percent_label=Widget(),
        _refresh_background_prepare_action_states=lambda: None,
        _background_prepare_is_current=lambda generation: generation == 7,
    )
    host._background_prepare_set_active_presentation = (
        lambda active, **kwargs: DZLLWindow._background_prepare_set_active_presentation(
            host, active, **kwargs,
        )
    )
    host._steam_ugc_format_size = (
        lambda value: DZLLWindow._steam_ugc_format_size(host, value)
    )
    for name in ("background_prepare_percent_label",):
        widget = getattr(host, name)
        widget.visible = True
        widget.opacity = 1.0
    return host


def test_active_strip_builder_has_fixed_flexible_optional_and_right_blocks():
    builder = WINDOW_SOURCE.split(
        "def _build_background_prepare_status_block", 1,
    )[1].split("def _background_prepare_set_active_presentation", 1)[0]
    assert "grid = Gtk.Grid()" in builder
    assert "root.append(grid)" in builder
    assert "grid.attach(info, 0, 0, 2, 1)" in builder
    assert "grid.attach(progress_row" not in builder
    assert "grid.set_margin_top(3)" in builder
    assert "grid.set_margin_bottom(3)" in builder
    assert "root.set_vexpand(False)" in builder
    assert "root.set_valign(Gtk.Align.START)" in builder
    assert "self.background_prepare_info = info" in builder
    assert "info.set_vexpand(False)" in builder
    assert "server_block.set_size_request(240, -1)" in builder
    assert "background_prepare_server_label.set_width_chars(30)" in builder
    assert "background_prepare_server_label.set_max_width_chars(30)" in builder
    assert builder.count("set_ellipsize(Pango.EllipsizeMode.END)") >= 2
    assert 'server_divider = Gtk.Label(label="•")' in builder
    assert builder.count('Gtk.Label(label="•")') == 2
    assert builder.index("server_divider") < builder.index(
        "background_prepare_detail_label"
    )
    assert "background_prepare_detail_label.set_hexpand(True)" in builder
    assert "background_prepare_queue_label.set_visible(False)" in builder
    assert 'background_prepare_queue_divider = Gtk.Label(label="•")' in builder
    assert "background_prepare_queue_divider.set_visible(False)" in builder
    assert builder.index("background_prepare_queue_divider") < builder.index(
        "background_prepare_queue_label"
    ) < builder.index("right = Gtk.Box")
    assert "right.set_size_request(108, -1)" in builder
    assert "right.set_halign(Gtk.Align.END)" in builder
    assert builder.index("background_prepare_count_label") < builder.index(
        "background_prepare_percent_label"
    ) < builder.index(
        "background_prepare_action_btn"
    )
    assert builder.index("background_prepare_action_btn") < builder.index(
        "return root"
    )
    assert 'connect(\n            "clicked", self._background_prepare_action_clicked' in builder
    assert "background_prepare_percent_label.set_size_request(44, -1)" in builder
    assert "background_prepare_percent_label.set_halign(Gtk.Align.FILL)" in builder
    assert "background_prepare_percent_label.set_valign(Gtk.Align.CENTER)" in builder
    assert "background_prepare_percent_label.set_opacity" not in builder
    assert "failed_slot" not in builder
    assert "BACKGROUND_PREPARE_STATUS_HEIGHT" not in WINDOW_SOURCE
    assert "FixedHeightStatusBox" not in WINDOW_SOURCE
    assert "Gtk.ProgressBar()" not in builder
    assert "right.append(self.background_prepare_percent_label)" in builder
    assert "dzll-background-prepare-progress trough" not in STYLE_SOURCE


def test_progress_fill_uses_container_class_and_clears_it_when_inactive():
    helper = WINDOW_SOURCE.split(
        "def _background_prepare_set_progress_presentation", 1,
    )[1].split("def _background_prepare_download_available", 1)[0]
    assert "root = self.background_prepare_status" in helper
    assert "root.remove_css_class(old_class)" in helper
    assert 'f"dzll-background-prepare-progress-{percent}"' in helper
    assert "root.add_css_class(progress_class)" in helper


def test_cancelling_keeps_action_button_allocation():
    for method_name, next_name in (
        ("def _background_prepare_apply_queue_snapshot", "def _background_prepare_start_frozen_request"),
        ("def _background_prepare_render_cancelling", "def _background_prepare_render_batch_summary"),
    ):
        source = WINDOW_SOURCE.split(method_name, 1)[1].split(next_name, 1)[0]
        assert "_background_prepare_present_cancelling(self)" in source

    helper = WINDOW_SOURCE.split(
        "def _background_prepare_set_action_presentation", 1,
    )[1].split("def _background_prepare_present_cancelling", 1)[0]
    assert "button.set_visible(True)" in helper
    assert "set_opacity(1.0 if actionable else 0.0)" in helper
    assert "button.set_sensitive(bool(actionable))" in helper


def test_active_queue_snapshot_uses_separate_server_and_optional_queue_blocks():
    queue = BackgroundPreparationQueue()
    active_server = _server("10.0.0.1", "A deliberately long server name")
    queued_server = _server("10.0.0.2", "Queued")
    queue.enqueue(_snapshot(active_server), _runtime())
    queue.enqueue(_snapshot(queued_server), _runtime())
    host = _host(queue)

    DZLLWindow._background_prepare_apply_queue_snapshot(host, queue.snapshot())

    assert host.background_prepare_server_label.text == active_server.name
    assert host.background_prepare_server_label.tooltip == active_server.name
    assert host.background_prepare_queue_label.text == "Queued Servers: 1"
    assert host.background_prepare_queue_label.visible
    assert host.background_prepare_queue_divider.visible
    assert "dzll-background-prepare-active" in host.background_prepare_status.css_classes
    assert host.background_prepare_action_btn.text == "Cancel"


def test_status_container_uses_search_row_inset_and_present_only_bottom_spacing():
    host = _host()

    DZLLWindow._background_prepare_set_active_presentation(
        host, True, server_name="Server", queued_count=0,
    )

    assert "search_row.set_margin_start(10)" in SIDEBAR_SOURCE
    assert "search_row.set_margin_end(10)" in SIDEBAR_SOURCE
    assert host.background_prepare_status.margin_start == 10
    assert host.background_prepare_status.margin_end == 10
    assert host.background_prepare_status.margin_bottom == 8

    DZLLWindow._background_prepare_set_active_presentation(host, False)

    assert host.background_prepare_status.margin_start == 0
    assert host.background_prepare_status.margin_end == 0
    assert host.background_prepare_status.margin_bottom == 0
    assert host.background_prepare_percent_label.text == ""
    assert host.background_prepare_percent_label.visible
    assert host.background_prepare_percent_label.opacity == 1.0


def test_active_mod_count_size_progress_and_percentage_are_presented_separately():
    host = _host()
    size = int(2.4 * 1024 * 1024 * 1024)
    snapshot = SimpleNamespace(
        current_mod_name="A very long Workshop mod name",
        stage_text="Downloading…",
        item_total_bytes=size,
        current=1,
        total=13,
        progress_mode=PreparationProgressMode.DETERMINATE,
        fraction=0.28,
    )

    DZLLWindow._background_prepare_render_snapshot(host, 7, snapshot)

    assert host.background_prepare_detail_label.text == (
        "Downloading Mod: A very long Workshop mod name (2.4 GB)"
    )
    assert host.background_prepare_count_label.text == "1/13"
    assert host.background_prepare_count_label.visible
    assert "dzll-background-prepare-progress-28" in host.background_prepare_status.css_classes
    assert host.background_prepare_percent_label.text == "28%"
    assert host.background_prepare_percent_label.visible
    assert host.background_prepare_percent_label.opacity == 1.0


def test_indeterminate_state_keeps_reserved_progress_row_visually_suppressed():
    host = _host()
    DZLLWindow._background_prepare_set_active_presentation(
        host, True, server_name="Server", queued_count=0,
    )
    snapshot = SimpleNamespace(
        current_mod_name="",
        stage_text="Waiting for Steam…",
        item_total_bytes=0,
        current=0,
        total=0,
        progress_mode=PreparationProgressMode.INDETERMINATE,
        fraction=None,
    )
    DZLLWindow._background_prepare_render_snapshot(host, 7, snapshot)

    assert not host.background_prepare_queue_label.visible
    assert not host.background_prepare_queue_divider.visible
    assert host.background_prepare_detail_label.text == "Waiting for Steam…"
    assert not host.background_prepare_count_label.visible
    assert host.background_prepare_percent_label.text == ""
    assert host.background_prepare_percent_label.visible
    assert host.background_prepare_percent_label.opacity == 1.0


def test_finished_and_cancelling_states_clear_percentage_without_collapsing_row():
    host = _host()
    host.background_prepare_percent_label.set_text("0%")
    batch = SimpleNamespace(
        ready_count=1,
        failed_count=0,
        cancelled_count=0,
        failed_entries=(),
    )

    DZLLWindow._background_prepare_render_batch_summary(host, batch)

    assert "dzll-background-prepare-present" in host.background_prepare_status.css_classes
    assert host.background_prepare_server_label.text == (
        "Background mod preparation finished"
    )
    assert host.background_prepare_detail_label.text == "1 ready · 0 failed"
    assert host.background_prepare_action_btn.text == "Close"
    assert host.background_prepare_action_btn.visible
    assert host.background_prepare_percent_label.visible
    assert host.background_prepare_percent_label.text == ""
    assert host.background_prepare_percent_label.opacity == 1.0

    queue = BackgroundPreparationQueue()
    transition = queue.enqueue(
        _snapshot(_server("10.0.0.1", "Server")), _runtime(),
    )
    assert queue.attach_controller(
        transition.dispatch, SimpleNamespace(cancel=lambda: True),
    )
    cancelling = queue.cancel_all().snapshot
    host = _host(queue)
    host.background_prepare_percent_label.set_text("0%")

    DZLLWindow._background_prepare_apply_queue_snapshot(host, cancelling)

    assert "dzll-background-prepare-present" in host.background_prepare_status.css_classes
    assert host.background_prepare_server_label.text == (
        "Cancelling background mod preparation…"
    )
    assert host.background_prepare_detail_label.text == (
        "Waiting for active Steam cleanup to finish."
    )
    assert host.background_prepare_right_status_label.text == "Cancelling…"
    assert host.background_prepare_right_status_label.visible
    assert host.background_prepare_action_btn.visible
    assert host.background_prepare_action_btn.opacity == 0.0
    assert not host.background_prepare_action_btn.sensitive
    assert host.background_prepare_percent_label.visible
    assert host.background_prepare_percent_label.text == ""
    assert host.background_prepare_percent_label.opacity == 1.0


def test_percentage_values_use_one_stable_right_aligned_slot():
    builder = WINDOW_SOURCE.split(
        "def _build_background_prepare_status_block", 1,
    )[1].split("def _background_prepare_set_active_presentation", 1)[0]
    assert "background_prepare_percent_label.set_size_request(44, -1)" in builder
    assert "background_prepare_percent_label.set_width_chars(4)" in builder
    assert "background_prepare_percent_label.set_max_width_chars(4)" in builder
    assert "background_prepare_percent_label.set_halign(Gtk.Align.FILL)" in builder

    host = _host()
    for fraction, expected in ((0.09, "9%"), (0.28, "28%"), (1.0, "100%")):
        snapshot = SimpleNamespace(
            current_mod_name="Mod",
            stage_text="Downloading…",
            item_total_bytes=1024,
            current=1,
            total=1,
            progress_mode=PreparationProgressMode.DETERMINATE,
            fraction=fraction,
        )
        DZLLWindow._background_prepare_render_snapshot(host, 7, snapshot)
        assert host.background_prepare_percent_label.text == expected
        assert (
            f"dzll-background-prepare-progress-{int(fraction * 100)}"
            in host.background_prepare_status.css_classes
        )


def test_progress_fill_covers_zero_partial_and_complete_without_changing_cancel():
    host = _host()
    host.background_prepare_action_btn.set_label("Cancel")
    for fraction, expected_class in (
        (0.0, "dzll-background-prepare-progress-0"),
        (0.42, "dzll-background-prepare-progress-42"),
        (1.0, "dzll-background-prepare-progress-100"),
    ):
        DZLLWindow._background_prepare_set_progress_presentation(
            host, True, fraction,
        )
        progress_classes = {
            name for name in host.background_prepare_status.css_classes
            if name.startswith("dzll-background-prepare-progress-")
        }
        assert progress_classes == {expected_class}
        assert host.background_prepare_action_btn.text == "Cancel"
        assert host.background_prepare_action_btn.sensitive


def test_panel_uses_standard_border_and_large_bullet_separators():
    assert ".dzll-background-prepare-status {{" in STYLE_SOURCE
    base_rule = STYLE_SOURCE.split(
        ".dzll-background-prepare-status {{", 1,
    )[1].split("}}", 1)[0]
    assert "background-color: @dzll_surface_control" in base_rule
    assert "background-color: alpha(@dzll_accent" not in base_rule
    assert "border: 1px solid @dzll_border" in base_rule
    assert "alpha(@dzll_focus" not in base_rule
    assert ".dzll-background-prepare-status .dzll-background-prepare-bullet" in STYLE_SOURCE
    assert "font-size: 1.25em" in STYLE_SOURCE
    assert ".dzll-background-prepare-active .dzll-background-prepare-divider" not in STYLE_SOURCE
    assert "@dzll_accent {percent}%" in STYLE_SOURCE
    assert "@dzll_surface_control {percent}%" in STYLE_SOURCE


def test_cancelling_status_text_is_neither_dimmed_nor_bold():
    rule = STYLE_SOURCE.split(
        ".dzll-background-prepare-status .dzll-background-prepare-right-status {{",
        1,
    )[1].split("}}", 1)[0]
    assert "color: @dzll_text_primary" in rule
    assert "font-weight: normal" in rule
    assert "@dzll_text_muted" not in rule
