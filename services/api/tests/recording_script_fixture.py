"""录制脚本单测只用显式临时文件，不依赖开发者Windows目录或实际设备。"""

import pytest


def recording_script_fixture(environment_key, filename):
    @pytest.fixture(autouse=True)
    def recorded_steps(monkeypatch, tmp_path):
        path = tmp_path / filename
        path.write_text(
            'print("===== start =====")\n'
            'capture_screen("fixture_01")\n'
            'capture_screen("fixture_02")\n'
            'tap_point(1309, 232)\n'
            'tap_point(82, 240)\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(environment_key, str(path))
        return path

    return recorded_steps
