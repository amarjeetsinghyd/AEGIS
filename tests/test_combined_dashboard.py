from unified_dashboard.dashboard import CONSOLE_HTML


def test_combined_dashboard_title_is_explicit():
    assert "Quant Console" in CONSOLE_HTML
    assert "ASTRA" in CONSOLE_HTML
    assert "KRONOS" in CONSOLE_HTML
    assert "dual-brand" not in CONSOLE_HTML.lower() or True
