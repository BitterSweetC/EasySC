import unittest

from app.screen_capture import (
    CAPTURE_MODE_ACTIVE_WINDOW,
    CAPTURE_MODE_SINGLE_MONITOR,
    MonitorInfo,
    ScreenCaptureConfig,
    get_monitor_by_index,
    normalize_bbox,
)


class ScreenCaptureTests(unittest.TestCase):
    def test_config_from_preset_keeps_target_fields(self) -> None:
        config = ScreenCaptureConfig.from_preset(
            "HD",
            capture_mode=CAPTURE_MODE_ACTIVE_WINDOW,
            monitor_index=2,
            window_handle=123,
            window_title="Demo Window",
        )
        self.assertEqual(config.capture_mode, CAPTURE_MODE_ACTIVE_WINDOW)
        self.assertEqual(config.monitor_index, 2)
        self.assertEqual(config.window_handle, 123)
        self.assertEqual(config.window_title, "Demo Window")
        self.assertEqual(config.jpeg_quality, 80)

    def test_normalize_bbox(self) -> None:
        self.assertEqual(normalize_bbox((0, 0, 100, 80)), (0, 0, 100, 80))
        with self.assertRaises(RuntimeError):
            normalize_bbox((10, 10, 10, 20))

    def test_get_monitor_by_index(self) -> None:
        monitors = [
            MonitorInfo(index=0, left=0, top=0, right=1920, bottom=1080, name="A"),
            MonitorInfo(index=1, left=1920, top=0, right=3840, bottom=1080, name="B"),
        ]
        self.assertEqual(get_monitor_by_index(monitors, 1).name, "B")
        with self.assertRaises(RuntimeError):
            get_monitor_by_index(monitors, 3)


if __name__ == "__main__":
    unittest.main()
