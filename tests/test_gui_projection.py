import unittest
from unittest import mock

from app.gui import ScreenCastingApp
from app.screen_capture import CAPTURE_MODE_ACTIVE_WINDOW, ScreenCaptureConfig


class GuiProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = ScreenCastingApp()
        self.app.withdraw()

    def tearDown(self) -> None:
        if self.app.winfo_exists():
            self.app.destroy()

    def test_select_projection_target_second_click_starts(self) -> None:
        self.app.start_selected_projection = mock.Mock()

        self.app.select_projection_target(CAPTURE_MODE_ACTIVE_WINDOW)
        self.assertEqual(self.app.projection_target_var.get(), CAPTURE_MODE_ACTIVE_WINDOW)
        self.app.start_selected_projection.assert_not_called()

        self.app.select_projection_target(CAPTURE_MODE_ACTIVE_WINDOW)
        self.app.start_selected_projection.assert_called_once()

    @mock.patch("app.gui.messagebox.showinfo")
    @mock.patch("app.gui.open_connect_panel")
    def test_open_connect_panel_shows_result_dialog_when_requested(self, open_panel: mock.Mock, showinfo: mock.Mock) -> None:
        self.app.open_connect_panel(show_result_dialog=True)

        open_panel.assert_called_once()
        showinfo.assert_called_once()
        self.assertIn("Win+K", self.app.status_var.get())

    @mock.patch("app.gui.messagebox.showerror")
    def test_apply_mirror_start_failed_shows_error_reason(self, showerror: mock.Mock) -> None:
        self.app._apply_mirror_start_failed("Window: Browser", "Test error")

        showerror.assert_called_once()
        self.assertIn("Test error", showerror.call_args.args[1])
        self.assertIn("Window: Browser", self.app.status_var.get())

    @mock.patch("app.gui.messagebox.showinfo")
    def test_apply_mirror_started_shows_success_dialog(self, showinfo: mock.Mock) -> None:
        config = ScreenCaptureConfig.from_preset("Standard", capture_mode=CAPTURE_MODE_ACTIVE_WINDOW, window_title="Browser")

        self.app._apply_mirror_started(config, "Window: Browser", "http://127.0.0.1:1234/")

        showinfo.assert_called_once()
        self.assertEqual(self.app.mirror_url_var.get(), "http://127.0.0.1:1234/")
        self.assertEqual(self.app.selected_window_var.get(), "Browser")
        self.assertIn("http://127.0.0.1:1234/", showinfo.call_args.args[1])


    @mock.patch("app.gui.messagebox.showerror")
    @mock.patch("app.gui.DlnaController")
    def test_cast_worker_keeps_player_url_when_dlna_fails(self, controller_cls: mock.Mock, showerror: mock.Mock) -> None:
        controller = controller_cls.return_value
        controller.set_media.side_effect = RuntimeError("DLNA failed")
        self.app._prepare_media_delivery_source = mock.Mock(return_value=("http://127.0.0.1:19000/media/demo.mp4", "http://127.0.0.1:19000/player", "https://source.example/demo.mp4"))
        device = mock.Mock()
        device.display_name = "Mock TV"

        self.app._cast_worker(device, "https://example.com/vod/play/100")
        self.app.update()

        showerror.assert_called_once()
        self.assertEqual(self.app.player_url_var.get(), "http://127.0.0.1:19000/player")
        self.assertEqual(self.app.media_url_var.get(), "http://127.0.0.1:19000/media/demo.mp4")
        self.assertIn("已保留兼容播放页地址", self.app.status_var.get())


if __name__ == "__main__":
    unittest.main()
