from dataclasses import FrozenInstanceError

import pytest

from dzll_launcher.preparation_contracts import (
    JoinPopupPreparationPresenter,
    NoOpPreparationPresenter,
    PreparationEventKind,
    PreparationOutcome,
    PreparationPresenter,
    PreparationProgressEvent,
    PreparationStatus,
)


def test_all_terminal_outcomes_are_representable_and_immutable():
    for status in PreparationStatus:
        outcome = PreparationOutcome(status=status, reason=status.value)
        assert outcome.status is status
        with pytest.raises(FrozenInstanceError):
            outcome.reason = "changed"
    assert set(PreparationStatus) == {
        PreparationStatus.READY,
        PreparationStatus.NO_REQUIRED_MODS,
        PreparationStatus.CANCELLED,
        PreparationStatus.FAILED,
    }
    supplied = [(7, "Original")]
    outcome = PreparationOutcome(PreparationStatus.READY, verified_mods=supplied)
    supplied.append((8, "Mutated"))
    assert outcome.verified_mods == ((7, "Original"),)


def test_progress_event_is_deeply_immutable_and_round_trips_authoritative_payload():
    payload = {
        "type": "session", "backend": "steam_ugc", "backend_owner": "steam_client",
        "join_attempt_id": 17, "id": 123, "name": "Exact Name",
        "download_bytes": 25, "total_bytes": 100,
        "state_names": ["Subscribed", "Downloading"],
        "nested": {"unchanged": True},
    }
    event = PreparationProgressEvent.from_authoritative_payload(payload)
    payload["state_names"].append("MUTATED")
    payload["nested"]["unchanged"] = False
    assert event.kind is PreparationEventKind.ITEM
    assert event.operation_id == 17
    assert event.item_id == 123
    assert event.item_name == "Exact Name"
    assert event.downloaded_bytes == 25
    assert event.total_bytes == 100
    assert event.fraction is None
    restored = event.authoritative_payload()
    assert restored["state_names"] == ["Subscribed", "Downloading"]
    assert restored["nested"] == {"unchanged": True}
    with pytest.raises(FrozenInstanceError):
        event.item_id = 999


def test_determinate_and_indeterminate_values_are_passed_through_not_calculated():
    determinate = PreparationProgressEvent.from_authoritative_payload({
        "id": 9, "download_bytes": 7, "total_bytes": 13, "fraction": 0.125,
        "indeterminate": False,
    })
    assert (determinate.downloaded_bytes, determinate.total_bytes) == (7, 13)
    assert determinate.fraction == 0.125
    assert determinate.indeterminate is False
    indeterminate = PreparationProgressEvent.from_authoritative_payload({
        "id": 9, "download_bytes": 0, "total_bytes": 0, "indeterminate": True,
    })
    assert indeterminate.fraction is None
    assert indeterminate.indeterminate is True


def test_noop_presenter_accepts_every_event_and_terminal_type():
    presenter = NoOpPreparationPresenter()
    assert PreparationPresenter.__name__ == "PreparationPresenter"
    for kind in PreparationEventKind:
        presenter.on_event(PreparationProgressEvent(kind=kind, operation_id=1))
    presenter.on_cancelling(1)
    for status in PreparationStatus:
        presenter.on_terminal(PreparationOutcome(status))


def test_join_popup_adapter_forwards_exact_payload_and_terminal_once():
    events = []
    cancelling = []
    terminals = []
    presenter = JoinPopupPreparationPresenter(
        consume_event=events.append,
        consume_cancelling=cancelling.append,
        consume_terminal=terminals.append,
    )
    payload = {
        "backend": "steam_ugc", "backend_owner": "steam_client",
        "join_attempt_id": 44, "id": 700, "name": "Authoritative",
        "event_source": "poll", "download_bytes": 10, "total_bytes": 20,
        "request_accepted": True,
    }
    presenter.on_event(PreparationProgressEvent.from_authoritative_payload(payload))
    presenter.on_cancelling(44)
    outcome = PreparationOutcome(PreparationStatus.CANCELLED, reason="user")
    presenter.on_terminal(outcome)
    presenter.on_terminal(PreparationOutcome(PreparationStatus.FAILED))
    assert events == [payload]
    assert cancelling == [44]
    assert terminals == [outcome]


def test_presenter_surface_contains_no_preparation_decisions():
    public = {name for name in dir(NoOpPreparationPresenter) if not name.startswith("_")}
    assert public == {"on_event", "on_cancelling", "on_terminal"}
    forbidden = {"start", "schedule", "poll", "retry", "select_item", "classify", "download"}
    assert public.isdisjoint(forbidden)


def test_stale_ownership_token_is_preserved_for_existing_owner_rejection():
    payload = {"join_attempt_id": 81, "backend_owner": "steam_client", "id": 5}
    event = PreparationProgressEvent.from_authoritative_payload(payload)
    assert event.operation_id == 81
    assert event.authoritative_payload()["join_attempt_id"] == 81
    accepted = []
    current_attempt = 82

    def existing_owner_consumer(candidate):
        if candidate.get("join_attempt_id") == current_attempt:
            accepted.append(candidate)

    JoinPopupPreparationPresenter(consume_event=existing_owner_consumer).on_event(event)
    assert accepted == []
