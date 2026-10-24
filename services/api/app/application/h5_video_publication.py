"""普通视频正式帧合同；沿用检查点实测，不从页型或几何猜播放成功。"""

from services.api.app.application.checkpoint_judgement import measure_checkpoint_series


def video_publication_is_ready(
    contract,
    *,
    screenshot_checksums=None,
    primary_screenshot_checksums=None,
):
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != "h5-video-publication.v1"
    ):
        return False
    samples = contract.get("samples")
    try:
        measured = measure_checkpoint_series(samples)
    except (TypeError, ValueError, KeyError, AttributeError, StopIteration):
        return False
    if measured.get("loaded_frame") is not True or measured.get("stable_frame") is not True:
        return False
    if (
        contract.get("primary_screenshot_sha256") != samples[-1]["screenshot_sha256"]
        or contract.get("page_url") != samples[-1]["snapshot"]["url"]
    ):
        return False
    if screenshot_checksums is not None:
        actual = set(screenshot_checksums)
        if any("sha256:" + item["screenshot_sha256"] not in actual for item in samples):
            return False
    if primary_screenshot_checksums is not None and list(primary_screenshot_checksums) != [
        "sha256:" + contract["primary_screenshot_sha256"]
    ]:
        return False
    return True
