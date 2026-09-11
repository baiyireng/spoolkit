from report import render_report


def test_output_unchanged():
    rows = [
        {"name": "a", "items": [{"price": 2, "count": 3}]},
        {"name": "b", "items": []},
        {"name": "c", "items": [{"price": 2000, "count": 1}]},
        {"name": "d", "items": [{"price": -5, "count": 3}]},
        {"name": "e", "items": [{"price": 100, "count": 2, "discount": 0.5}]},
    ]
    assert render_report(rows) == (
        "a: 6\nb: 0 (empty)\nc: 2000 [large]\nd: 0 (empty)\ne: 100.0\ntotal: 2106.0"
    )


def test_render_report_is_short():
    import inspect

    from report import render_report as fn

    source = inspect.getsource(fn)
    assert len(source.splitlines()) < 20
