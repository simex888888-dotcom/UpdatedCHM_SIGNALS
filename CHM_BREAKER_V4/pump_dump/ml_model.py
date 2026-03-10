"""
ml_model.py — ML фильтр на основе XGBoost (25 признаков).

Работает в двух режимах:
  - Нет model.pkl → возвращает None, агрегатор работает без ML слоя
  - model.pkl есть → предсказывает pump/dump/neutral вероятности

Автопереобучение каждые 7 дней из накопленной истории сигналов.
"""

import logging
import os
import pickle
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from pump_dump.pd_config import (
    ML_MODEL_PATH, ML_RETRAIN_DAYS, ML_MIN_SAMPLES,
    ML_PUMP_THRESHOLD, ML_PRECISION_MIN,
)

log = logging.getLogger("CHM.PD.ML")

# Метки классов
LABEL_PUMP    = 1
LABEL_DUMP    = 2
LABEL_NEUTRAL = 0


@dataclass
class MLResult:
    pump_prob: float
    dump_prob: float
    predicted: str    # "PUMP" | "DUMP" | "NEUTRAL"
    confidence: float
    precision: float  # precision модели на последнем валидационном наборе


class PDMLModel:
    def __init__(self):
        self._model     = None
        self._precision = 0.0
        self._loaded    = False
        self._last_try  = 0.0
        self._load()

    def _load(self):
        if not os.path.exists(ML_MODEL_PATH):
            return
        try:
            with open(ML_MODEL_PATH, "rb") as f:
                bundle = pickle.load(f)
            self._model     = bundle["model"]
            self._precision = bundle.get("precision", 0.0)
            self._loaded    = True
            log.info(f"✅ PD ML модель загружена, precision={self._precision:.2f}")
        except Exception as e:
            log.warning(f"PD ML load: {e}")

    def is_ready(self) -> bool:
        return self._loaded and self._model is not None and self._precision >= ML_PRECISION_MIN

    def predict(self, features: list[float]) -> Optional[MLResult]:
        if not self.is_ready():
            return None
        try:
            X  = np.array(features, dtype=float).reshape(1, -1)
            pr = self._model.predict_proba(X)[0]
            # pr = [neutral_prob, pump_prob, dump_prob]
            pump_p = float(pr[LABEL_PUMP])    if len(pr) > LABEL_PUMP    else 0.0
            dump_p = float(pr[LABEL_DUMP])    if len(pr) > LABEL_DUMP    else 0.0
            if pump_p >= ML_PUMP_THRESHOLD:
                return MLResult(pump_p, dump_p, "PUMP",    pump_p, self._precision)
            if dump_p >= ML_PUMP_THRESHOLD:
                return MLResult(pump_p, dump_p, "DUMP",    dump_p, self._precision)
            return MLResult(pump_p, dump_p, "NEUTRAL", max(pump_p, dump_p), self._precision)
        except Exception as e:
            log.debug(f"PD ML predict: {e}")
            return None

    async def maybe_retrain(self, db_path: str):
        """
        Запускаем переобучение если прошло ML_RETRAIN_DAYS и накоплено
        достаточно данных с известным исходом (pd_outcomes).
        """
        if time.time() - self._last_try < 3600:
            return
        self._last_try = time.time()

        try:
            import aiosqlite
            async with aiosqlite.connect(db_path) as conn:
                cur = await conn.execute(
                    "SELECT features_json, actual_label FROM pd_train_data "
                    "WHERE actual_label IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 10000"
                )
                rows = await cur.fetchall()

            if len(rows) < ML_MIN_SAMPLES:
                log.debug(f"PD ML: недостаточно данных ({len(rows)}/{ML_MIN_SAMPLES})")
                return

            import json
            X, y = [], []
            for feat_json, label in rows:
                try:
                    X.append(json.loads(feat_json))
                    y.append(int(label))
                except Exception:
                    pass

            if len(X) < ML_MIN_SAMPLES:
                return

            self._train(np.array(X), np.array(y))
        except Exception as e:
            log.warning(f"PD ML retrain: {e}")

    def _train(self, X: np.ndarray, y: np.ndarray):
        try:
            from xgboost import XGBClassifier
            from sklearn.model_selection import train_test_split
            from sklearn.metrics import precision_score

            X_tr, X_val, y_tr, y_val = train_test_split(X, y, test_size=0.2, random_state=42)
            model = XGBClassifier(
                n_estimators=500, max_depth=6, learning_rate=0.01,
                subsample=0.8, colsample_bytree=0.8,
                scale_pos_weight=10,
                use_label_encoder=False, eval_metric="mlogloss",
                verbosity=0,
            )
            model.fit(X_tr, y_tr)
            y_pred = model.predict(X_val)
            # precision только для pump+dump классов
            prec = precision_score(y_val, y_pred, labels=[LABEL_PUMP, LABEL_DUMP],
                                   average="macro", zero_division=0)
            bundle = {"model": model, "precision": prec, "trained_at": time.time()}
            with open(ML_MODEL_PATH, "wb") as f:
                pickle.dump(bundle, f)
            self._model     = model
            self._precision = prec
            self._loaded    = True
            log.info(f"✅ PD ML переобучена: precision={prec:.2f}, samples={len(X)}")
        except ImportError:
            log.warning("xgboost/sklearn не установлены — ML слой отключён")
        except Exception as e:
            log.warning(f"PD ML train error: {e}")


def build_feature_vector(an, ob, hs, ind) -> list[float]:
    """
    Строит вектор 25 признаков из результатов всех анализаторов.
    an  — AnomalyResult
    ob  — OBResult
    hs  — HiddenResult
    ind — IndicatorResult
    """
    return [
        # Базовые
        float(an.volume_zscore),
        float(an.price_change_1m),
        float(an.price_change_3m),
        float(an.price_change_3m * 1.67),          # приближение 5m
        float(1 - ob.imbalance),                    # buy_ratio = 1 - ask_ratio
        float(ob.imbalance),
        float(ob.spread_pct),
        # Технические
        float(ind.rsi),
        float(int(ind.rsi_divergence)),
        float(ind.macd_histogram),
        float(ind.bb_width_pct),
        float(ind.volume_ma_ratio),
        float(an.atr_pct),
        float(ind.vwap_deviation),
        # Скрытые сигналы
        float(hs.funding_rate * 10000),             # в базисных пунктах
        float(hs.funding_delta_bonus),
        float(hs.oi_change_10m * 100),
        float(int(hs.oi_signal)),
        float(an.cvd_10),
        float(int(hs.cvd_divergence)),
        float(hs.long_short_ratio),
        float(0.0),                                 # liquidation_zone_distance (нет Coinglass)
        # Контекст
        float(__import__("datetime").datetime.utcnow().hour),
        float(abs(an.price_change_3m) * 100),       # приближение volatility_24h
        float(ob.spread_pct),
    ]


# Синглтон модели
_model = PDMLModel()


def get_model() -> PDMLModel:
    return _model
