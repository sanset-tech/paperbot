"""Caricamento e validazione della configurazione."""
import json
from pathlib import Path


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = "config.json") -> dict:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_base_dir"] = str(path.resolve().parent)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    r, s, c = cfg["risk"], cfg["strategy"], cfg["costs"]
    checks = [
        (len(cfg["universe"]) > 0, "universe vuoto"),
        (0 < r["risk_per_trade"] <= 0.03, "risk_per_trade deve stare tra 0 e 3%"),
        (0 < r["max_position_pct"] <= 1, "max_position_pct fuori range"),
        (0 < r["max_gross_exposure"] <= 1, "niente leva: max_gross_exposure <= 1"),
        (r["max_positions"] >= 1, "max_positions >= 1"),
        (0 < r["drawdown_reduce_at"] < r["drawdown_kill_at"] < 1, "soglie drawdown incoerenti"),
        (s.get("type", "breakout") in ("breakout", "rotation"), "strategy.type: breakout o rotation"),
        (s.get("type") == "rotation" or s["exit_breakout"] < s["entry_breakout"] < s["trend_sma"], "periodi strategia incoerenti"),
        (s.get("type") == "rotation" or (s["initial_stop_atr"] > 0 and s["trailing_stop_atr"] > 0), "stop ATR devono essere > 0"),
        (s.get("type") != "rotation" or s["top_n"] >= 1, "top_n >= 1"),
        (0 <= c.get("cash_rate_fallback", 0) < 0.2, "cash_rate_fallback fuori range"),
        (c["commission_pct"] >= 0 and c["slippage_pct"] >= 0, "costi negativi"),
        (cfg["portfolio"]["initial_capital"] > 0, "capitale iniziale <= 0"),
    ]
    for ok, msg in checks:
        if not ok:
            raise ConfigError(msg)


def resolve(cfg: dict, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg.get("_base_dir", ".")) / p
