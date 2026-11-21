import pytest

from src.orchestrator import idea_bundle, idea_commitment as ic


def _raw(**extra):
    base = {
        "idea_id": "i1",
        "title": "T",
        "hypothesis": "H",
        "implementation_plan": ["a"],
        "target_rubrics": [{"id": "m", "description": "d", "type": "metric"}],
    }
    base.update(extra)
    return base


PRED = {"metric": "combined_score", "expected_delta": 0.02, "confidence": 0.6}
FALS = {"metric": "combined_score", "refuted_if": "<=", "threshold": 0.9, "description": "no gain"}


def test_bundle_without_registration_is_unchanged():
    out = idea_bundle.normalize_idea_bundle(_raw(), 1)
    assert "prediction" not in out and "falsifier" not in out


def test_bundle_keeps_valid_registration():
    out = idea_bundle.normalize_idea_bundle(_raw(prediction=PRED, falsifier=FALS), 1)
    assert out["falsifier"]["threshold"] == 0.9 and out["prediction"]["confidence"] == 0.6


@pytest.mark.parametrize(
    "bad",
    [
        {"description": "if it does not work well"},
        {**FALS, "refuted_if": "worse"},
        {**FALS, "threshold": "0.9"},
        {**FALS, "description": " "},
    ],
)
def test_vague_falsifier_is_rejected(bad):
    with pytest.raises(idea_bundle.IdeaBundleValidationError):
        idea_bundle.normalize_idea_bundle(_raw(falsifier=bad), 1)


def test_confidence_out_of_range_rejected():
    with pytest.raises(idea_bundle.IdeaBundleValidationError):
        idea_bundle.normalize_idea_bundle(_raw(prediction={**PRED, "confidence": 1.5}), 1)


def test_commitment_detects_post_hoc_edit():
    bundle = idea_bundle.normalize_idea_bundle(_raw(prediction=PRED, falsifier=FALS), 1)
    commitment = ic.build_commitment(bundle, committed_at="t")
    assert ic.verify_commitment(bundle, commitment)
    bundle["falsifier"] = {**bundle["falsifier"], "threshold": 0.1}
    assert not ic.verify_commitment(bundle, commitment)


def test_notes_edits_do_not_break_commitment():
    bundle = idea_bundle.normalize_idea_bundle(_raw(prediction=PRED, falsifier=FALS), 1)
    commitment = ic.build_commitment(bundle, committed_at="t")
    bundle["notes_for_agent"] = "changed"
    assert ic.verify_commitment(bundle, commitment)


@pytest.mark.parametrize(
    "score,expected",
    [(0.85, "refuted"), (0.9, "refuted"), (0.95, "not_refuted"), (None, "unmeasured")],
)
def test_evaluate_falsifier(score, expected):
    assert ic.evaluate_falsifier(FALS, metrics=None, combined_score=score) == expected


def test_unregistered_idea():
    assert ic.evaluate_falsifier(None, metrics={}, combined_score=1.0) == "unregistered"


def test_calibration_summary():
    bundle = idea_bundle.normalize_idea_bundle(_raw(prediction=PRED, falsifier=FALS), 1)
    c = ic.build_commitment(bundle, committed_at="t")
    rows = [
        ic.ledger_record(bundle, cycle_index=i, attempt_id=f"a{i}", commitment=c,
                         commitment_intact=True, metrics=None, combined_score=s, baseline_score=0.9)
        for i, s in enumerate([0.95, 0.8])
    ]
    summary = ic.calibration_summary(rows)
    assert summary["scored"] == 2 and summary["buckets"][0]["hit_rate"] == 0.5
    assert summary["brier_score"] == pytest.approx(((0.6 - 1) ** 2 + 0.6**2) / 2)


def test_prompt_summary_empty_and_populated(tmp_path):
    assert "No scored predictions" in ic.summarize_prediction_calibration(tmp_path)
    bundle = idea_bundle.normalize_idea_bundle(_raw(prediction=PRED, falsifier=FALS), 1)
    c = ic.build_commitment(bundle, committed_at="t")
    import json

    row = ic.ledger_record(bundle, cycle_index=1, attempt_id="a", commitment=c,
                           commitment_intact=True, metrics=None, combined_score=0.8,
                           baseline_score=0.9)
    (tmp_path / ic.LEDGER_FILE).write_text(json.dumps(row) + "\nnot json\n")
    text = ic.summarize_prediction_calibration(tmp_path)
    assert "Brier" in text and "0% survived" in text


def test_require_preregistration_rejects_unregistered_bundle(tmp_path):
    import json

    from src.investigation.idea_generation import IdeaArtifactsError, load_idea_artifacts

    def read_json(path):
        return json.loads(path.read_text())

    rubrics = _raw()["target_rubrics"]
    (tmp_path / "idea_bundle.json").write_text(json.dumps(_raw()))
    (tmp_path / "target_rubrics.json").write_text(json.dumps(rubrics))
    assert load_idea_artifacts(tmp_path, 1, read_json).bundle["idea_id"] == "i1"
    with pytest.raises(IdeaArtifactsError) as err:
        load_idea_artifacts(tmp_path, 1, read_json, require_preregistration=True)
    assert err.value.reason == "missing_preregistration"
    (tmp_path / "idea_bundle.json").write_text(json.dumps(_raw(prediction=PRED, falsifier=FALS)))
    assert "falsifier" in load_idea_artifacts(
        tmp_path, 1, read_json, require_preregistration=True
    ).bundle
