#!/usr/bin/env python3
from pathlib import Path
import unittest

from dzll_launcher.join_popup_presentation import (
    JoinPopupActivityTracker,
    JoinPopupPhase,
    JoinPopupPresentation,
    JoinPopupPresentationController,
    ugc_activity_presentation,
)


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SOURCE = (ROOT / "src/dzll_launcher/window.py").read_text(encoding="utf-8")
JOIN_SOURCE = (ROOT / "src/dzll_launcher/join_prepare.py").read_text(encoding="utf-8")
STEAMCMD_UI_SOURCE = (ROOT / "src/dzll_launcher/join_preparation_overlay_ui.py").read_text(encoding="utf-8")


class TurnScheduler:
    def __init__(self):
        self.callbacks = []

    def __call__(self, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def flush_turn(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


class PresentationHarness:
    def __init__(self, attempt_id=1):
        self.attempt_id = attempt_id
        self.commits = []
        self.scheduler = TurnScheduler()
        self.controller = JoinPopupPresentationController(
            schedule=self.scheduler,
            commit=self.commits.append,
            is_current_attempt=lambda attempt_id: attempt_id == self.attempt_id,
        )

    def state(self, phase, text, *, backend="steam_client", item_key=None, attempt_id=None):
        return JoinPopupPresentation(
            self.attempt_id if attempt_id is None else attempt_id,
            backend,
            phase,
            text,
            item_key,
        )


class JoinPopupPresentationTests(unittest.TestCase):
    def test_all_ready_queue_has_no_per_item_checking_lines(self):
        states = [
            ugc_activity_presentation(
                {"id": mid, "name": f"Mod {mid}", "installed": True, "ready": True},
                attempt_id=1,
            )
            for mid in range(1, 16)
        ]
        self.assertEqual(states, [None] * 15)
        self.assertNotIn("Checking Mod", WINDOW_SOURCE)

    def test_one_missing_item_stays_silent_until_byte_progress(self):
        tracker = JoinPopupActivityTracker()
        state = ugc_activity_presentation(
            {"id": 10, "name": "Missing Mod", "installed": False,
             "event_source": "initial", "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(state)
        state = ugc_activity_presentation(
            {"id": 10, "name": "Missing Mod", "installed": False,
             "event_source": "request", "request_attempted": True,
             "request_accepted": True, "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(state)
        state = ugc_activity_presentation(
            {"id": 10, "name": "Missing Mod", "installed": False,
             "event_source": "poll", "request_attempted": True,
             "request_accepted": True, "download_bytes": 1,
             "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertEqual((state.phase, state.text),
                         (JoinPopupPhase.DOWNLOADING, "Downloading Missing Mod…"))

    def test_one_outdated_item_stays_silent_until_byte_progress(self):
        tracker = JoinPopupActivityTracker()
        state = ugc_activity_presentation(
            {"id": 11, "name": "Old Mod", "installed": True, "needs_update": True,
             "event_source": "initial", "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(state)
        state = ugc_activity_presentation(
            {"id": 11, "name": "Old Mod", "installed": True, "needs_update": True,
             "event_source": "request", "request_attempted": True,
             "request_accepted": True, "was_installed_before": True,
             "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(state)
        state = ugc_activity_presentation(
            {"id": 11, "name": "Old Mod", "installed": True, "needs_update": True,
             "event_source": "poll", "request_attempted": True,
             "request_accepted": True, "was_installed_before": True,
             "download_bytes": 1, "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertEqual((state.phase, state.text),
                         (JoinPopupPhase.UPDATING, "Updating Old Mod…"))

    def test_mixed_queue_ignores_ready_items(self):
        events = [
            {"id": 1, "name": "Ready", "installed": True, "ready": True},
            {"id": 2, "name": "Missing", "installed": False},
            {"id": 3, "name": "Old", "installed": True, "needs_update": True},
        ]
        tracker = JoinPopupActivityTracker()
        states = [ugc_activity_presentation(
            event | {"event_source": "initial", "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        ) for event in events]
        self.assertEqual(states, [None, None, None])

    def test_fifteen_item_snapshot_commits_at_most_once_per_turn(self):
        harness = PresentationHarness()
        for mid in range(1, 16):
            state = ugc_activity_presentation(
                {"id": mid, "name": f"Mod {mid}", "installed": False,
                 "event_source": "initial", "backend_owner": "steam_client"}, attempt_id=1
            )
            if state is not None:
                harness.controller.request(state)
        self.assertEqual(harness.commits, [])
        self.assertEqual(len(harness.scheduler.callbacks), 0)
        harness.scheduler.flush_turn()
        self.assertEqual(len(harness.commits), 0)

    def test_identical_initial_and_request_snapshots_render_once(self):
        harness = PresentationHarness()
        state = harness.state(JoinPopupPhase.DOWNLOADING, "Downloading Mod…", item_key=7)
        harness.controller.request(state)
        harness.controller.request(state)
        harness.scheduler.flush_turn()
        harness.controller.request(state)
        self.assertEqual(len(harness.commits), 1)

    def test_request_burst_ending_at_rendered_state_drops_stale_intermediate(self):
        harness = PresentationHarness()
        final = harness.state(JoinPopupPhase.UPDATING, "Updating Final…", item_key=9)
        harness.controller.request(final)
        harness.scheduler.flush_turn()
        harness.controller.request(
            harness.state(JoinPopupPhase.DOWNLOADING, "Downloading Earlier…", item_key=8)
        )
        harness.controller.request(final)
        harness.scheduler.flush_turn()
        self.assertEqual([state.text for state in harness.commits], ["Updating Final…"])

    def test_final_ready_transition_does_not_replace_activity(self):
        harness = PresentationHarness()
        tracker = JoinPopupActivityTracker()
        initial = ugc_activity_presentation(
            {"id": 7, "name": "Mod", "installed": False, "event_source": "initial",
             "backend_owner": "steam_client"}, attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(initial)
        active = ugc_activity_presentation(
            {"id": 7, "name": "Mod", "installed": False, "download_pending": True,
             "event_source": "request", "request_attempted": True,
             "request_accepted": True, "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(active)
        active = ugc_activity_presentation(
            {"id": 7, "name": "Mod", "installed": False,
             "event_source": "poll", "request_attempted": True,
             "request_accepted": True, "download_bytes": 1,
             "backend_owner": "steam_client"}, attempt_id=1, tracker=tracker,
        )
        harness.controller.request(active)
        harness.scheduler.flush_turn()
        ready = ugc_activity_presentation(
            {"id": 7, "name": "Mod", "installed": True, "ready": True,
             "event_source": "final", "backend_owner": "steam_client"},
            attempt_id=1, tracker=tracker,
        )
        self.assertIsNone(ready)
        self.assertEqual([state.text for state in harness.commits], ["Downloading Mod…"])

    def test_post_download_internal_steps_do_not_replace_lifecycle_message(self):
        self.assertNotIn('"Preparing DayZ launch…"', JOIN_SOURCE)
        self.assertNotIn('"Preparing required mods..."', JOIN_SOURCE)
        self.assertIn("_join_popup_show_launching(attempt_id)", JOIN_SOURCE)

    def test_shared_overlay_has_no_steamcmd_credentials_or_size_scanner(self):
        self.assertNotIn("SteamCMD Login", STEAMCMD_UI_SOURCE)
        self.assertNotIn("Steam Guard", STEAMCMD_UI_SOURCE)
        self.assertNotIn("_steamcmd_refresh_active_download_line2", STEAMCMD_UI_SOURCE)
        self.assertIn("join_preparation_cancel_button", STEAMCMD_UI_SOURCE)

    def test_errors_bypass_pending_coalescing(self):
        harness = PresentationHarness()
        harness.controller.request(harness.state(JoinPopupPhase.CHECKING, "Checking…"))
        error = harness.state(JoinPopupPhase.ERROR, "Disk full")
        harness.controller.render_immediate(error)
        self.assertEqual([state.text for state in harness.commits], ["Disk full"])
        harness.scheduler.flush_turn()
        self.assertEqual([state.text for state in harness.commits], ["Disk full"])

    def test_cancellation_bypasses_pending_and_preserves_button_update(self):
        harness = PresentationHarness()
        harness.controller.request(harness.state(JoinPopupPhase.DOWNLOADING, "Downloading…"))
        harness.controller.render_immediate(
            harness.state(JoinPopupPhase.CANCELLING, "Cancelling download and cleaning up…")
        )
        harness.scheduler.flush_turn()
        self.assertEqual(len(harness.commits), 1)
        self.assertIn("_steam_client_set_cancel_buttons(safe_cancel=True)", WINDOW_SOURCE)

    def test_stale_attempt_callback_cannot_overwrite_new_attempt(self):
        harness = PresentationHarness(attempt_id=1)
        old = harness.state(JoinPopupPhase.DOWNLOADING, "Old", attempt_id=1)
        harness.controller.request(old)
        harness.attempt_id = 2
        new = harness.state(JoinPopupPhase.CHECKING, "New", attempt_id=2)
        harness.controller.request(new)
        harness.scheduler.flush_turn()
        self.assertEqual([state.text for state in harness.commits], ["New"])
        self.assertFalse(harness.controller.request(old))

    def test_popup_labels_are_not_backend_control_state(self):
        control_reads = [
            "join_preparation_detail_label.get_text",
            "join_preparation_status_label.get_text",
            "join_preparation_heading.get_text",
        ]
        for read in control_reads:
            self.assertNotIn(read, WINDOW_SOURCE)
            self.assertNotIn(read, JOIN_SOURCE)

    def test_join_diagnostics_remain_present(self):
        self.assertIn("def _join_log", WINDOW_SOURCE)
        self.assertIn('"UGC helper shutdown completed"', JOIN_SOURCE)
        self.assertIn('"post-download filesystem verification completed"', JOIN_SOURCE)
        self.assertIn('"Steam handoff submitted"', WINDOW_SOURCE)

    def test_duplicate_suppression_is_attempt_and_backend_aware(self):
        harness = PresentationHarness(attempt_id=1)
        first = harness.state(JoinPopupPhase.CHECKING, "Checking…", backend="steam_client")
        harness.controller.request(first)
        harness.scheduler.flush_turn()
        different_backend = harness.state(JoinPopupPhase.CHECKING, "Checking…", backend="steamcmd")
        harness.controller.request(different_backend)
        harness.scheduler.flush_turn()
        harness.attempt_id = 2
        different_attempt = harness.state(
            JoinPopupPhase.CHECKING, "Checking…", backend="steam_client", attempt_id=2
        )
        harness.controller.request(different_attempt)
        harness.scheduler.flush_turn()
        self.assertEqual(len(harness.commits), 3)

    def test_real_work_progress_percentage_path_remains(self):
        reducer = (ROOT / "src/dzll_launcher/preparation_presentation.py").read_text()
        renderer = WINDOW_SOURCE.split("def _render_join_preparation_snapshot", 1)[1]
        renderer = renderer.split("def _open_steam_downloads", 1)[0]
        self.assertIn("float(download_bytes) / float(total_bytes)", reducer)
        self.assertIn("join_preparation_progress_bar.set_fraction(fraction)", renderer)
        self.assertIn('percent_label.set_text(f"{int(fraction * 100)}%")', renderer)

    def test_backend_work_and_join_continuation_are_unchanged(self):
        self.assertIn("ok = win.run_steam_client_install(", JOIN_SOURCE)
        self.assertNotIn("run_steamcmd_install", JOIN_SOURCE)
        self.assertIn("win.GLib.idle_add(after)", JOIN_SOURCE)
        self.assertNotIn("sleep(", JOIN_SOURCE)
        shared = JOIN_SOURCE.split("def prepare_required_mods", 1)[1].split(
            "def join_prepare_and_launch", 1
        )[0]
        self.assertEqual(shared.count("retry_ok = win.run_steam_client_install("), 1)
        self.assertIn("mod_ids=unresolved_ids", shared)
        self.assertNotIn("prepare_required_mods(", shared)
        self.assertNotIn("while ", shared)
        self.assertNotIn("ensure_watch_symlinks", shared)
        self.assertNotIn("bootstrap_launcher_state", shared)
        self.assertNotIn("_launch_direct_steam_url", shared)
        self.assertEqual(JOIN_SOURCE.count("win._launch_direct_steam_url("), 1)


if __name__ == "__main__":
    unittest.main()
