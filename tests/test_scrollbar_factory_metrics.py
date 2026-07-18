from dzll_launcher import scrollbar_interaction as interaction_module
from dzll_launcher.scrollbar_interaction import (
    SCROLLBAR_FACTORY_NAMES,
    ScrollbarFactoryMetrics,
)


def test_disabled_metrics_never_read_clock_or_count(monkeypatch):
    monkeypatch.setattr(
        interaction_module.time,
        "perf_counter_ns",
        lambda: (_ for _ in ()).throw(AssertionError("clock must stay unused")),
    )
    metrics = ScrollbarFactoryMetrics(False)
    assert not metrics.begin(1)
    assert metrics.start("name", "bind") is None
    assert not metrics.record_setup("name")
    assert not metrics.count("name_text_writes")
    assert metrics.settle(1) is None


def test_new_interaction_starts_with_all_empty_factories():
    metrics = ScrollbarFactoryMetrics(True)
    assert metrics.begin(7)
    assert tuple(metrics.factories) == SCROLLBAR_FACTORY_NAMES
    assert all(vars(item) == {
        "setup_count": 0,
        "bind_count": 0,
        "unbind_count": 0,
        "bind_total_ns": 0,
        "bind_max_ns": 0,
        "unbind_total_ns": 0,
        "unbind_max_ns": 0,
        "light_bind_count": 0,
        "full_bind_count": 0,
        "light_bind_total_ns": 0,
        "light_bind_max_ns": 0,
    } for item in metrics.factories.values())
    assert metrics.operations == {}


def test_lifecycle_counts_totals_maxima_and_factory_identity(monkeypatch):
    values = iter((100, 150, 200, 500, 600, 650))
    monkeypatch.setattr(interaction_module.time, "perf_counter_ns", lambda: next(values))
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(3)
    assert metrics.record_setup("name")
    assert metrics.record_setup("ping")
    assert metrics.finish(metrics.start("name", "bind"))
    assert metrics.finish(metrics.start("players", "bind"))
    assert metrics.finish(metrics.start("ping", "unbind"))
    snapshot = metrics.settle(3)

    assert snapshot.generation == 3
    assert snapshot.factories["name"].bind_count == 1
    assert snapshot.factories["name"].bind_total_ns == 50
    assert snapshot.factories["players"].bind_max_ns == 300
    assert snapshot.factories["ping"].unbind_total_ns == 50
    max_bind = max(
        snapshot.factories.items(), key=lambda item: item[1].bind_max_ns
    )
    assert max_bind[0] == "players"


def test_operation_counters_accumulate_and_stale_finish_is_rejected(monkeypatch):
    values = iter((10, 20))
    monkeypatch.setattr(interaction_module.time, "perf_counter_ns", lambda: next(values))
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(4)
    token = metrics.start("name", "bind")
    metrics.count("name_text_writes", 7)
    metrics.count("name_text_writes", 2)
    metrics.count("notify_connects", 3)
    snapshot = metrics.settle(4)

    assert snapshot.operations == {"name_text_writes": 9, "notify_connects": 3}
    assert not metrics.finish(token)
    assert metrics.settle(3) is None


def test_new_generation_does_not_inherit_prior_metrics():
    metrics = ScrollbarFactoryMetrics(True)
    metrics.begin(1)
    metrics.record_setup("name")
    metrics.count("name_tooltip_writes", 5)
    assert metrics.settle(1)

    metrics.begin(2)
    assert metrics.generation == 2
    assert metrics.factories["name"].setup_count == 0
    assert metrics.operations == {}
