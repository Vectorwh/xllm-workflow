"""Fresh delivery and replay contracts, entirely offline."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/xllm-release-validation/scripts"))
import delivery_reproduction as delivery
import release_common as common
import release_prepare
import release


@pytest.fixture
def row():
    config = common.load_config(common.ASSETS / "release.example.yaml")
    case = common.expand(config)[3]
    case.update(case_id=case["id"], mode="xtask")
    case["xtask"]["commit"] = "b" * 40
    case["delivery"]["xtask_reproduction"]["image"] = "fixture/xllm:release"
    return case


def release_id():
    return {"version": "0.12.0", "commit": "a" * 40}


def test_fresh_delivery_preserves_workload_without_old_environment(row):
    row["timeout"] = 1234
    original = deepcopy(row)
    task = delivery.generate(row, release_id())
    fresh = task["release_reproduction"]["spec"]
    text = yaml.safe_dump(task)
    for old in (row["code"]["repo"], row["code"]["binary"], row["code"]["build_verdict"], row["model"]["draft_model"]):
        assert old not in text
    assert row == original
    assert task["deployment"]["instances"][0]["reuse_existing_container"] is False
    assert fresh["preparation"]["containers"][0]["mode"] == "create"
    assert "reference" not in fresh["preparation"]["containers"][0]
    assert fresh["cases"][0]["workload"] == row["workload"]
    assert fresh["cases"][0]["service"]["flags"] == row["service"]["flags"]
    assert fresh["cases"][0]["code"]["commit"] == "a" * 40
    assert fresh["preparation"]["sources"][1]["commit"] == "a" * 40
    assert fresh["preparation"]["builds"] and fresh["preparation"]["mtp"]
    assert "@@REPO@@" not in yaml.safe_dump(fresh["preparation"])
    assert fresh["cases"][0]["xtask"]["exact_config"] is True
    assert fresh["cases"][0]["timeout"] == 1234


def test_replay_resolves_only_new_paths_and_rejects_mutation(row, tmp_path):
    task = delivery.generate(row, release_id())
    spec = delivery.replay_spec(task, tmp_path / "new")
    case = common.expand(spec)[0]
    container = spec["preparation"]["containers"][0]
    assert container["name"] != row["environment"]["service"]["container"]
    assert case["environment"]["service"]["container"] == container["name"]
    assert case["code"]["repo"].startswith(str(tmp_path / "new/source"))
    assert str(tmp_path / "new") + ":" + str(tmp_path / "new") in container["binds"]
    assert "@@" not in yaml.safe_dump({k: v for k, v in spec.items() if k != "cases"})
    changed = deepcopy(task)
    changed["benchmark"]["perf"]["batch_size"] = 8
    with pytest.raises(ValueError, match="configuration changed"):
        delivery.replay_spec(changed, tmp_path / "other")
    (tmp_path / "occupied").mkdir()
    (tmp_path / "occupied/file").touch()
    with pytest.raises(ValueError, match="empty"):
        delivery.replay_spec(task, tmp_path / "occupied")


def test_plan_only_uses_fresh_build_without_docker_or_git_operations(row, tmp_path, monkeypatch):
    task = delivery.generate(row, release_id())
    source = tmp_path / "case.yaml"
    source.write_text(yaml.safe_dump(task))
    def unexpected(*args, **kwargs):
        pytest.fail("plan-only must perform no preparation or workload")
    monkeypatch.setattr(release_prepare, "prepare_campaign", unexpected)
    monkeypatch.setattr(release, "execute", unexpected)
    result = delivery.reproduce(source, tmp_path / "fresh", plan_only=True)
    assert result == {"run_root": str(tmp_path / "fresh"), "cases": 1, "executed": False}
    plan = release.load_plan(tmp_path / "fresh")
    assert plan["config"]["preparation"]["builds"]
    assert plan["cases"][0]["code"]["binary"].startswith(str(tmp_path / "fresh/source/xllm"))
    assert (tmp_path / "fresh/delivery-input.yaml").read_text() == source.read_text()


def test_reproduction_orders_prepare_execute_report(row, tmp_path, monkeypatch):
    source = tmp_path / "case.yaml"
    source.write_text(yaml.safe_dump(delivery.generate(row, release_id())))
    calls = []
    monkeypatch.setattr(release_prepare, "prepare_campaign", lambda *a: calls.append("prepare"))
    monkeypatch.setattr(release, "execute", lambda *a: calls.append("execute"))
    monkeypatch.setattr(release, "report", lambda *a: calls.append("report"))
    result = delivery.reproduce(source, tmp_path / "fresh")
    assert calls == ["prepare", "execute", "report"] and result["executed"]


@pytest.mark.parametrize("missing", ["image", "source_url", "build", "mtp"])
def test_missing_recipe_does_not_fall_back_to_original_container(row, missing):
    row["delivery"]["xtask_reproduction"].pop(missing)
    with pytest.raises(ValueError):
        delivery.generate(row, release_id())


def test_delivery_rejects_container_reference_and_absolute_artifact_path(row):
    profile = row["delivery"]["xtask_reproduction"]
    profile["container"]["reference"] = "old-running-container"
    with pytest.raises(ValueError, match="existing container"):
        delivery.generate(row, release_id())
    del profile["container"]["reference"]
    profile["build"]["binary"] = row["code"]["binary"]
    with pytest.raises(ValueError, match="relative"):
        delivery.generate(row, release_id())


def test_reproduction_still_requires_configured_activation_evidence(row, tmp_path):
    import direct_adapter
    row["service"]["activation_checks"] = {"graph": "graph activated", "mtp": "mtp activated"}
    case = common.expand(delivery.fresh_spec(row, release_id()))[0]
    assert not case.get("comparison_group")
    with pytest.raises(ValueError, match="activation evidence"):
        direct_adapter.check_activation(case, tmp_path, tmp_path)
    (tmp_path / "node_0.log").write_text("graph activated\nmtp activated")
    assert direct_adapter.check_activation(case, tmp_path, tmp_path)["status"] == "PASS"


def test_existing_raw_assets_need_no_container_or_binary(row, tmp_path, monkeypatch):
    data = tmp_path / "weights"
    data.mkdir()
    (data / "config.json").write_text("{}")
    config = {"path": str(data), "sha256": {"config.json": common.file_hash(data / "config.json")}}
    assert release_prepare.prepare_resource(config, tmp_path)["path"] == str(data)
    (data / "config.json").write_text("changed")
    with pytest.raises(ValueError, match="identity"):
        release_prepare.prepare_resource(config, tmp_path)


def test_exact_xtask_jsons_do_not_inherit_local_checkout_defaults(row, tmp_path, monkeypatch):
    import xtask_adapter
    spec = delivery.replay_spec(delivery.generate(row, release_id()), tmp_path / "fresh")
    case = common.expand(spec)[0]
    repo = Path(case["xtask"]["repo"])
    (repo / "config").mkdir(parents=True)
    for name in ("xTask_config.json", "xllm_config.json"):
        (repo / "config" / name).write_text('{"DO_NOT_INHERIT": "old-container-path"}')
    attempt = tmp_path / "run/cases/one/attempt-001"
    attempt.mkdir(parents=True)
    # Stop before native imports; the effective JSON files have already been written.
    monkeypatch.setattr(xtask_adapter, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop-preview")))
    with pytest.raises(RuntimeError, match="stop-preview"):
        xtask_adapter.prepare(case, attempt)
    for path in (attempt / "xtask/config").glob("*.json"):
        assert "DO_NOT_INHERIT" not in path.read_text()
