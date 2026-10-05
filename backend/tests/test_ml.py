from app.ml.dataset import generate
from app.services.state import STATE
from tests.conftest import CRITICAL_CASE, MILD_CASE


def test_dataset_is_deterministic():
    a, b = generate(200, seed=5), generate(200, seed=5)
    assert a.equals(b)
    assert set(a["severity"]) <= {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_model_loaded_and_predicts():
    assert STATE.model.available
    p = STATE.model.predict(CRITICAL_CASE)
    assert p.severity == "CRITICAL" and abs(sum(p.probabilities.values()) - 1) < 1e-3
    assert STATE.model.predict(MILD_CASE).severity in ("LOW", "MEDIUM")


def test_metrics_report_exists():
    m = STATE.model.metrics()
    assert m["dataset"]["split"] == "80/20 stratified"
    for name in ("logistic_regression", "random_forest", "gradient_boosting"):
        assert {"accuracy", "precision_macro", "recall_macro", "f1_macro", "confusion_matrix"} <= set(m["models"][name])
