"""Concurrent traffic events must not each retrain the traffic model (CPU storm that stalled the API)."""
import threading

from app.services import traffic_prediction as tp


def test_concurrent_callers_train_once(monkeypatch):
    pred = tp.TrafficPredictor()
    calls = []
    real = pred.retrain

    def slow_retrain():
        calls.append(1)
        return real()

    monkeypatch.setattr(pred, "retrain", slow_retrain)
    threads = [threading.Thread(target=pred.ensure_model) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and pred.model is not None
    pred.ensure_model()
    assert len(calls) == 1                     # fresh model reused
    pred.ensure_model(force=True)
    assert len(calls) == 2                     # explicit retrain still possible
