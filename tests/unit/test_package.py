def test_package_exposes_version() -> None:
    import trading_house

    assert trading_house.__version__ == "0.1.0"
