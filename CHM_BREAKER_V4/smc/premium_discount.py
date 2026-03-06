"""
smc/premium_discount.py — Premium / Discount Zones & Equilibrium
"""
import pandas as pd
import logging

log = logging.getLogger("CHM.SMC.PremiumDiscount")


def get_premium_discount(swing_high: float,
                         swing_low: float,
                         current_price: float,
                         buffer_pct: float = 2.0) -> dict:
    """
    Диапазон: swing_low → swing_high
    Equilibrium = (high + low) / 2
    Discount = ниже equilibrium - buffer
    Premium  = выше equilibrium + buffer
    position_pct: позиция цены в диапазоне (0 = swing_low, 100 = swing_high)
    """
    if swing_high <= swing_low:
        return {
            "zone":         "NEUTRAL",
            "position_pct": 50.0,
            "equilibrium":  current_price,
            "premium_above": current_price,
            "discount_below": current_price,
        }
    full_range   = swing_high - swing_low
    equilibrium  = (swing_high + swing_low) / 2
    buffer       = full_range * buffer_pct / 100

    premium_above  = equilibrium + buffer
    discount_below = equilibrium - buffer
    position_pct   = (current_price - swing_low) / full_range * 100

    if current_price >= premium_above:
        zone = "PREMIUM"
    elif current_price <= discount_below:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"

    log.debug(
        f"PD: pos={position_pct:.1f}% zone={zone} "
        f"eq={equilibrium:.4f} price={current_price:.4f}"
    )
    return {
        "zone":           zone,
        "position_pct":   round(position_pct, 1),
        "equilibrium":    equilibrium,
        "premium_above":  premium_above,
        "discount_below": discount_below,
        "swing_high":     swing_high,
        "swing_low":      swing_low,
    }
