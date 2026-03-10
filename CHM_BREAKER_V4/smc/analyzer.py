"""
smc/analyzer.py — SMC Analysis Orchestrator
Шаги: Structure → Liquidity → OB → FVG → Premium/Discount
"""
import logging
import pandas as pd
from typing import Optional

from .structure        import get_market_structure
from .liquidity        import find_liquidity_sweeps
from .order_block      import get_order_blocks, check_ob_mitigation
from .fvg              import get_fvg_analysis
from .premium_discount import get_premium_discount

log = logging.getLogger("CHM.SMC.Analyzer")


class SMCConfig:
    """Конфиг для SMC-анализа (все параметры из промта)."""
    # Structure
    SWING_LOOKBACK:     int   = 10
    BOS_CONFIRMATION:   bool  = True
    CHOCH_ENABLED:      bool  = True
    # Liquidity
    EQUAL_THRESHOLD_PCT: float = 0.05
    SWEEP_WICK_RATIO:    float = 0.6
    SWEEP_CLOSE_REQUIRED: bool = True
    # Order Block
    OB_MIN_IMPULSE_PCT:  float = 0.3
    OB_MAX_AGE_CANDLES:  int   = 50
    OB_MITIGATED_INVALID: bool = True
    OB_USE_BREAKER:      bool  = True
    # FVG
    FVG_ENABLED:         bool  = True
    FVG_MIN_GAP_PCT:     float = 0.1
    FVG_INVERSED:        bool  = True
    FVG_PARTIAL_INVALID: bool  = False
    # Premium/Discount
    PD_ENABLED:          bool  = True
    PD_BUFFER_PCT:       float = 2.0
    # Signal
    MIN_CONFIRMATIONS:   int   = 3
    MIN_RR:              float = 2.0
    SL_BUFFER_PCT:       float = 0.15
    TP1_RATIO:           float = 0.33
    TP2_RATIO:           float = 0.50
    TP3_RATIO:           float = 0.17


class SMCAnalyzer:
    """Оркестратор: принимает DataFrames (HTF, MTF, LTF) → dict анализа."""

    def __init__(self, config: Optional[SMCConfig] = None):
        self.cfg = config or SMCConfig()

    def analyze(self, symbol: str,
                df_htf: pd.DataFrame,
                df_mtf: pd.DataFrame,
                df_ltf: Optional[pd.DataFrame] = None) -> dict:
        """
        Запускает все 5 шагов SMC-анализа.
        Возвращает полный словарь анализа.
        """
        cfg = self.cfg
        result: dict = {
            "symbol":        symbol,
            "structure":     {},
            "liquidity":     {},
            "ob":            {},
            "fvg":           {},
            "pd_zone":       {},
            "error":         None,
            "atr":           0.0,
            "current_price": 0.0,
        }
        try:
            # ── Шаг 1: Market Structure (HTF) ─────────────────────────────
            structure = get_market_structure(
                df_htf,
                lookback      = cfg.SWING_LOOKBACK,
                bos_confirm   = cfg.BOS_CONFIRMATION,
                choch_enabled = cfg.CHOCH_ENABLED,
            )
            result["structure"] = structure

            # ── Шаг 2: Liquidity Sweeps (HTF) ─────────────────────────────
            liquidity = find_liquidity_sweeps(
                df_htf,
                structure["swing_highs"],
                structure["swing_lows"],
                threshold_pct    = cfg.EQUAL_THRESHOLD_PCT,
                close_required   = cfg.SWEEP_CLOSE_REQUIRED,
                wick_ratio       = cfg.SWEEP_WICK_RATIO,
            )
            result["liquidity"] = liquidity

            # ── Шаг 3: Order Blocks (MTF) ─────────────────────────────────
            ob = get_order_blocks(
                df_mtf,
                structure["bos"],
                min_impulse_pct    = cfg.OB_MIN_IMPULSE_PCT,
                max_age_candles    = cfg.OB_MAX_AGE_CANDLES,
                mitigated_invalid  = cfg.OB_MITIGATED_INVALID,
                use_breaker_blocks = cfg.OB_USE_BREAKER,
            )
            result["ob"] = ob

            # ── Шаг 4: FVG / IFVG (MTF или LTF) ──────────────────────────
            df_fvg = df_ltf if df_ltf is not None else df_mtf
            if cfg.FVG_ENABLED:
                fvg = get_fvg_analysis(
                    df_fvg,
                    min_gap_pct          = cfg.FVG_MIN_GAP_PCT,
                    inversed_fvg         = cfg.FVG_INVERSED,
                    partial_fill_invalid = cfg.FVG_PARTIAL_INVALID,
                )
            else:
                fvg = {"bull_fvg": None, "bear_fvg": None,
                       "bull_found": False, "bear_found": False}
            result["fvg"] = fvg

            # ── ATR (MTF) ──────────────────────────────────────────────────
            try:
                h = df_mtf["high"]; l = df_mtf["low"]
                pc = df_mtf["close"].shift(1)
                tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
                atr_s = tr.ewm(span=14, adjust=False).mean()
                result["atr"] = float(atr_s.iloc[-1]) if len(atr_s) > 0 else 0.0
            except Exception:
                result["atr"] = 0.0

            # ── Шаг 5: Premium / Discount ─────────────────────────────────
            last_sh = structure.get("last_swing_high")
            last_sl = structure.get("last_swing_low")
            if last_sh and last_sl and cfg.PD_ENABLED:
                current_price = float(df_mtf["close"].iloc[-1])
                result["current_price"] = current_price
                pd_zone = get_premium_discount(
                    last_sh["price"], last_sl["price"],
                    current_price, cfg.PD_BUFFER_PCT,
                )
            else:
                pd_zone = {"zone": "NEUTRAL", "position_pct": 50.0}
            result["pd_zone"] = pd_zone

            log.debug(
                f"{symbol}: trend={structure['trend']} "
                f"bos={structure['bos']['detected']} "
                f"pd={pd_zone['zone']}"
            )
        except Exception as e:
            log.error(f"{symbol}: SMC analyze error: {e}")
            result["error"] = str(e)
        return result
