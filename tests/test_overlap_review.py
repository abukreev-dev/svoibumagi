"""Source-cell comparison for private overlap diagnostics. SPDX-License-Identifier: GPL-3.0-or-later"""

from svoibumagi.source_match import raw_equivalent


def test_xls_xlsx_raw_cells_with_same_values_are_equivalent():
    xlsx = [None, "2026-02-06T19:34:53", "Комиссия", "-15", None, None]
    xls = ["", 46059.8158912037, "Комиссия", "-15"]
    assert raw_equivalent(xlsx, xls)
    assert not raw_equivalent(xlsx, ["", 46059.8158912037, "Комиссия", "-16"])
    assert not raw_equivalent([50000, "Комиссия"], ["2026-11-21T00:00:00", "Комиссия"])
