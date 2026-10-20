from app.config import settings


def test_mock_mode_toggle():
    assert isinstance(settings.MOCK_SENSORS, bool)
