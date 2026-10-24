"""旧分类取证合同只注入历史用例，不恢复生产包名路由。"""

from pathlib import Path

import pytest


@pytest.fixture
def legacy_home_top_features(monkeypatch):
    from services.api.app.application import app_ui_tree_features as features

    original = features.load_app_ui_tree_features
    frozen_path = str(Path(__file__).parents[1] / "fixtures/app_home_top_pre_orchestrator.json")

    def load_frozen(path=None, *, playbook_id=None, package_name=None):
        if path is None and (
            package_name == "dark.wwcskrpojs.ebjhyebjbl" or playbook_id == "hjll"
        ):
            return original(path=frozen_path)
        return original(path, playbook_id=playbook_id, package_name=package_name)

    monkeypatch.setattr(features, "load_app_ui_tree_features", load_frozen)
