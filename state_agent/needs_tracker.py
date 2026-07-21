"""
Needs Tracker — computes Mila's internal needs from real runtime data.

This module is completely separate from the core bot.
Loaded by Mila_observatory for testing purposes only.
"""

import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_agent.train_state_agent import MLP, predict, NUM_FEATURES, ACTIONS

STATE_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(STATE_AGENT_DIR, "state_agent.pt")


class NeedsTracker:
    """Tracks Mila's needs and runs state agent inference."""

    def __init__(self):
        self._model = None
        self._mean = None
        self._std = None
        self._model_loaded = False
        self._load_error = None

    def load_model(self) -> bool:
        if self._model_loaded:
            return True
        try:
            if not os.path.exists(MODEL_PATH):
                self._load_error = f"Model not found at {MODEL_PATH}"
                return False
            ckpt = torch.load(MODEL_PATH, map_location="cpu", weights_only=False)
            self._model = MLP(
                NUM_FEATURES,
                ckpt["config"]["hidden_dims"],
                len(ACTIONS),
                ckpt["config"]["dropout"],
            )
            self._model.load_state_dict(ckpt["state_dict"])
            self._mean = ckpt["mean"]
            self._std = ckpt["std"]
            self._model_loaded = True
            self._load_error = None
            return True
        except Exception as e:
            self._load_error = str(e)
            return False

    def get_model_info(self) -> dict:
        return {
            "loaded": self._model_loaded,
            "error": self._load_error,
            "model_path": MODEL_PATH,
            "num_features": NUM_FEATURES,
            "actions": ACTIONS,
        }

    def compute_needs(self) -> dict:
        """
        Compute needs from real runtime data and heuristics.

        Returns:
            dict with all features, needs, and metadata
        """
        now = datetime.now()
        current_hour = now.hour

        seconds_since_last_msg = self._get_seconds_since_last_msg()
        in_active_dialog = seconds_since_last_msg < 300

        fatigue = self._compute_fatigue(current_hour, seconds_since_last_msg, in_active_dialog)
        need_social = self._compute_need_social(current_hour, seconds_since_last_msg)
        boredom = self._compute_boredom(current_hour, seconds_since_last_msg)

        return {
            "seconds_since_last_msg": seconds_since_last_msg,
            "current_hour": current_hour,
            "fatigue": round(fatigue, 4),
            "need_social": round(need_social, 4),
            "boredom": round(boredom, 4),
            "in_active_dialog": in_active_dialog,
            "timestamp": now.isoformat(),
        }

    def run_inference(self, needs: Optional[dict] = None) -> dict:
        """Run ML model inference. If needs not provided, compute them."""
        if not self._model_loaded:
            self.load_model()

        if needs is None:
            needs = self.compute_needs()

        features = [
            needs["seconds_since_last_msg"],
            needs["current_hour"],
            needs["fatigue"],
            needs["need_social"],
            needs["boredom"],
            int(needs["in_active_dialog"]),
        ]

        if self._model_loaded and self._model is not None:
            start = time.perf_counter()
            action, probs = predict(self._model, self._mean, self._std, features)
            elapsed = time.perf_counter() - start
            probs_dict = {n: round(float(p), 4) for n, p in zip(ACTIONS, probs)}
            return {
                "action": action,
                "probabilities": probs_dict,
                "inference_time_ms": round(elapsed * 1000, 2),
                "model_loaded": True,
                "fallback": False,
            }
        else:
            return self._rule_based_inference(needs)

    def _rule_based_inference(self, needs: dict) -> dict:
        fatigue = needs["fatigue"]
        need_social = needs["need_social"]
        boredom = needs["boredom"]
        seconds = needs["seconds_since_last_msg"]
        hour = needs["current_hour"]
        in_dialog = needs["in_active_dialog"]

        if fatigue > 0.8 and (hour >= 22 or hour <= 5):
            action = "sleep"
        elif seconds > 30000 and 6 <= hour <= 10:
            action = "plan_day"
        elif in_dialog and (need_social > 0.5 or boredom > 0.5):
            action = "write_to_user"
        elif need_social > 0.7 and not in_dialog:
            action = "write_to_user"
        else:
            action = "wait"

        return {
            "action": action,
            "probabilities": {a: 1.0 if a == action else 0.0 for a in ACTIONS},
            "inference_time_ms": 0,
            "model_loaded": False,
            "fallback": True,
        }

    def _get_seconds_since_last_msg(self) -> float:
        try:
            from core.db import get_conn
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT time_start FROM chunks ORDER BY time_start DESC LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
            if row and row["time_start"]:
                last_time = datetime.fromisoformat(row["time_start"])
                last_time = last_time.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - last_time).total_seconds()
        except Exception:
            pass
        return 86400.0

    @staticmethod
    def _compute_fatigue(hour: int, seconds_since_last_msg: float, in_active_dialog: bool) -> float:
        if 6 <= hour < 12:
            base = 0.1 + 0.15 * (hour - 6) / 6
        elif 12 <= hour < 18:
            base = 0.25 + 0.2 * (hour - 12) / 6
        elif 18 <= hour < 22:
            base = 0.45 + 0.25 * (hour - 18) / 4
        else:
            if hour >= 22:
                base = 0.7 + 0.25 * (hour - 22) / 8
            else:
                base = 0.75 + 0.2 * hour / 6

        if in_active_dialog:
            base += 0.05
        if seconds_since_last_msg > 36000:
            base += 0.1

        return min(1.0, max(0.05, base))

    @staticmethod
    def _compute_need_social(hour: int, seconds_since_last_msg: float) -> float:
        if 8 <= hour < 22:
            need = 0.3 + 0.5 * (1 - 1 / (1 + seconds_since_last_msg / 1800))
            if seconds_since_last_msg > 3600:
                need += 0.1
            if seconds_since_last_msg > 7200:
                need += 0.1
        else:
            need = 0.1 + 0.3 * (1 - 1 / (1 + seconds_since_last_msg / 3600))

        return min(1.0, max(0.05, need))

    @staticmethod
    def _compute_boredom(hour: int, seconds_since_last_msg: float) -> float:
        if 8 <= hour < 23:
            boredom = 0.2 + 0.6 * (1 - 1 / (1 + seconds_since_last_msg / 1200))
            if seconds_since_last_msg > 1800:
                boredom += 0.1
            if seconds_since_last_msg > 3600:
                boredom += 0.1
        else:
            boredom = 0.1 + 0.3 * (1 - 1 / (1 + seconds_since_last_msg / 1800))

        return min(1.0, max(0.05, boredom))


_tracker: Optional[NeedsTracker] = None


def get_tracker() -> NeedsTracker:
    global _tracker
    if _tracker is None:
        _tracker = NeedsTracker()
    return _tracker
