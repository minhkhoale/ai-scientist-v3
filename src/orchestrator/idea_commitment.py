"""Pre-registered predictions, falsifiers, and the prediction ledger for ideas.

An idea may carry a ``prediction`` (what the agent expects) and a ``falsifier``
(a measurable result that would refute the hypothesis). Both are hash-committed
before the attempt runs, and the attempt outcome is scored against the committed
falsifier, not against the agent's later summary.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

FALSIFIER_OPERATORS = ("<", "<=", ">", ">=")
DEFAULT_METRIC = "combined_score"
COMMITTED_FIELDS = ("idea_id", "hypothesis", "target_rubrics", "prediction", "falsifier")

FalsifierOutcome = Literal["refuted", "not_refuted", "unmeasured", "unregistered"]


class CommitmentError(ValueError):
    """Prediction or falsifier is malformed, vague, or was altered after commit."""


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CommitmentError(f"{label} must be a finite number")
    return float(value)


def normalize_prediction(raw: Any) -> dict[str, Any]:
    """Validate ``{metric, expected_delta, confidence}``; confidence is in [0, 1]."""
    if not isinstance(raw, dict):
        raise CommitmentError("prediction must be an object")
    metric = raw.get("metric", DEFAULT_METRIC)
    if not isinstance(metric, str) or not metric.strip():
        raise CommitmentError("prediction.metric must be a non-empty string")
    confidence = _finite_number(raw.get("confidence"), "prediction.confidence")
    if not 0.0 <= confidence <= 1.0:
        raise CommitmentError("prediction.confidence must be in [0, 1]")
    return {
        "metric": metric.strip(),
        "expected_delta": _finite_number(raw.get("expected_delta"), "prediction.expected_delta"),
        "confidence": confidence,
    }


def normalize_falsifier(raw: Any) -> dict[str, Any]:
    """Validate ``{metric, refuted_if, threshold, description}``.

    The falsifier is machine-checkable on purpose: a vague condition ("if it does
    not work well") would restore the post-hoc reframing it exists to prevent.
    The threshold is absolute, in the same units as the evaluator metric.
    """
    if not isinstance(raw, dict):
        raise CommitmentError("falsifier must be an object")
    metric = raw.get("metric", DEFAULT_METRIC)
    if not isinstance(metric, str) or not metric.strip():
        raise CommitmentError("falsifier.metric must be a non-empty string")
    operator = raw.get("refuted_if")
    if operator not in FALSIFIER_OPERATORS:
        raise CommitmentError(f"falsifier.refuted_if must be one of {FALSIFIER_OPERATORS}")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise CommitmentError("falsifier.description must be a non-empty string")
    return {
        "metric": metric.strip(),
        "refuted_if": operator,
        "threshold": _finite_number(raw.get("threshold"), "falsifier.threshold"),
        "description": description.strip(),
    }


def commitment_digest(bundle: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the committed fields of a bundle."""
    payload = {key: bundle.get(key) for key in COMMITTED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_commitment(bundle: dict[str, Any], *, committed_at: str) -> dict[str, Any]:
    """Return the record to persist next to the idea before the attempt runs."""
    return {
        "idea_id": bundle["idea_id"],
        "committed_at": committed_at,
        "sha256": commitment_digest(bundle),
        "registered": "prediction" in bundle and "falsifier" in bundle,
    }


def verify_commitment(bundle: dict[str, Any], commitment: dict[str, Any]) -> bool:
    """True when the bundle still hashes to the digest committed before the attempt."""
    return commitment.get("sha256") == commitment_digest(bundle)


def evaluate_falsifier(
    falsifier: dict[str, Any] | None,
    *,
    metrics: dict[str, Any] | None,
    combined_score: float | None,
) -> FalsifierOutcome:
    """Score an attempt against the committed falsifier."""
    if falsifier is None:
        return "unregistered"
    metric = falsifier["metric"]
    value: Any = (metrics or {}).get(metric)
    if value is None and metric == DEFAULT_METRIC:
        value = combined_score
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "unmeasured"
    threshold = falsifier["threshold"]
    refuted = {
        "<": value < threshold,
        "<=": value <= threshold,
        ">": value > threshold,
        ">=": value >= threshold,
    }[falsifier["refuted_if"]]
    return "refuted" if refuted else "not_refuted"


def ledger_record(
    bundle: dict[str, Any],
    *,
    cycle_index: int,
    attempt_id: str,
    commitment: dict[str, Any],
    commitment_intact: bool,
    metrics: dict[str, Any] | None,
    combined_score: float | None,
    baseline_score: float | None,
) -> dict[str, Any]:
    """One predicted-versus-actual row for ``prediction_ledger.jsonl``."""
    prediction = bundle.get("prediction")
    falsifier = bundle.get("falsifier")
    actual_delta = (
        combined_score - baseline_score
        if combined_score is not None and baseline_score is not None
        else None
    )
    return {
        "cycle_index": cycle_index,
        "idea_id": bundle["idea_id"],
        "attempt_id": attempt_id,
        "commitment_sha256": commitment.get("sha256"),
        "committed_at": commitment.get("committed_at"),
        "commitment_intact": commitment_intact,
        "prediction": prediction,
        "falsifier": falsifier,
        "actual_score": combined_score,
        "baseline_score": baseline_score,
        "actual_delta": actual_delta,
        "falsifier_outcome": evaluate_falsifier(
            falsifier, metrics=metrics, combined_score=combined_score
        ),
    }


def calibration_summary(rows: list[dict[str, Any]], *, bins: int = 5) -> dict[str, Any]:
    """Confidence-binned hit rate: did high-confidence ideas survive their falsifier?

    ``hit`` means the falsifier was not triggered. Rows without a measured outcome
    or a prediction are counted but excluded from the bins.
    """
    scored = [
        row
        for row in rows
        if row.get("falsifier_outcome") in ("refuted", "not_refuted")
        and isinstance(row.get("prediction"), dict)
        and row.get("commitment_intact", False)
    ]
    buckets: list[dict[str, Any]] = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [
            row
            for row in scored
            if low <= row["prediction"]["confidence"] < high
            or (index == bins - 1 and row["prediction"]["confidence"] == 1.0)
        ]
        if not members:
            continue
        hits = sum(row["falsifier_outcome"] == "not_refuted" for row in members)
        buckets.append(
            {
                "confidence_range": [low, high],
                "count": len(members),
                "hit_rate": hits / len(members),
                "mean_confidence": sum(row["prediction"]["confidence"] for row in members)
                / len(members),
            }
        )
    brier = (
        sum(
            (row["prediction"]["confidence"] - (row["falsifier_outcome"] == "not_refuted")) ** 2
            for row in scored
        )
        / len(scored)
        if scored
        else None
    )
    return {
        "rows": len(rows),
        "scored": len(scored),
        "unregistered": sum(row.get("falsifier_outcome") == "unregistered" for row in rows),
        "tampered": sum(not row.get("commitment_intact", True) for row in rows),
        "brier_score": brier,
        "buckets": buckets,
    }


def summarize_calibration_for_prompt(rows: list[dict[str, Any]]) -> str:
    """Short prompt text showing the agent how its past predictions fared."""
    summary = calibration_summary(rows)
    if not summary["scored"]:
        return "_No scored predictions yet. State a calibrated `confidence`; it will be checked._"
    lines = [
        f"Scored predictions: {summary['scored']} of {summary['rows']} attempts; "
        f"Brier score {summary['brier_score']:.3f} (0 is perfect, 0.25 is a coin flip).",
    ]
    for bucket in summary["buckets"]:
        low, high = bucket["confidence_range"]
        lines.append(
            f"- stated confidence {low:.1f}-{high:.1f}: {bucket['count']} ideas, "
            f"{bucket['hit_rate']:.0%} survived their falsifier"
        )
    if summary["tampered"]:
        lines.append(f"- {summary['tampered']} attempts changed their committed bundle (excluded).")
    return "\n".join(lines)


LEDGER_FILE = "prediction_ledger.jsonl"


def summarize_prediction_calibration(results_dir: Path) -> str:
    """Prompt text built from ``results_dir/prediction_ledger.jsonl`` (empty ledger is fine)."""
    rows: list[dict[str, Any]] = []
    try:
        lines = (results_dir / LEDGER_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return summarize_calibration_for_prompt(rows)
