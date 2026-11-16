"""Track bound server-list rows so browser-live refresh can target real rows.

GTK keeps far more rows bound (realised) than are on screen, and its list
adjustment is only an estimate when row heights differ, so neither the bound
set nor ``adjustment value / row height`` identifies the visible rows. The
ColumnView name-column factory registers each bound cell here; on a refresh
tick :meth:`BoundRowTracker.visible_positions` keeps the cells whose bounds
overlap the viewport and reports their model positions.

GTK-thread only. Holds references only to currently bound cells.
"""

from __future__ import annotations

INVALID_LIST_POSITION = 0xFFFFFFFF


class BoundRowTracker:
    def __init__(self) -> None:
        self._entries: dict[int, tuple[object, object]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, cell, list_item) -> None:
        if cell is None or list_item is None:
            return
        self._entries[id(cell)] = (cell, list_item)

    def discard(self, cell) -> None:
        if cell is None:
            return
        self._entries.pop(id(cell), None)

    def clear(self) -> None:
        self._entries.clear()

    def visible_positions(self, viewport, model) -> list[int]:
        """Sorted model positions of bound rows overlapping ``viewport``.

        A row qualifies only if its cell is mapped, its bounds relative to the
        viewport overlap ``[0, viewport height)``, its list item reports a
        valid in-range position, and the model item at that position is the
        item the cell is bound to (guards against a rebuild in progress).
        """
        if viewport is None or model is None or not self._entries:
            return []
        try:
            height = float(viewport.get_height())
            total = int(model.get_n_items())
        except Exception:
            return []
        if height <= 0.0 or total <= 0:
            return []
        positions = set()
        for cell, list_item in list(self._entries.values()):
            try:
                if not cell.get_mapped():
                    continue
                ok, rect = cell.compute_bounds(viewport)
                if not ok:
                    continue
                top = float(rect.get_y())
                bottom = top + float(rect.get_height())
                if bottom <= 0.0 or top >= height:
                    continue
                position = int(list_item.get_position())
                if position == INVALID_LIST_POSITION or position < 0 or position >= total:
                    continue
                item = list_item.get_item()
                if item is None or model.get_item(position) is not item:
                    continue
            except Exception:
                continue
            positions.add(position)
        return sorted(positions)
