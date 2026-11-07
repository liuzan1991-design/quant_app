"""Local-only account exposure warnings, with no strategy permission changes."""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path

from core.qmt_snapshot import number, validate_snapshot
from core.qmt_state_review import _write_atomic

STRATEGY_REFERENCE = 300000.0


def account_exposure(snapshot, symbol, *, account_warning_fraction=0.70, reference_warning_fraction=0.70):
    """Both ratios use ACCOUNT symbol value, with separately named denominators."""
    result = dict(symbol=symbol, as_of=snapshot.get("as_of"), status="data_unavailable", alerts=[],
                  account_symbol_value=None, account_total_equity=None, account_equity_fraction=None,
                  strategy_reference_equity=STRATEGY_REFERENCE, account_value_vs_strategy_reference=None,
                  account_warning_fraction=account_warning_fraction, reference_warning_fraction=reference_warning_fraction,
                  affects_strategy_permissions=False, strategy_cap_changed=False)
    if not snapshot.get("valid"):
        result["alerts"] = ["SNAPSHOT_INVALID_DATA_NOT_CLEARED"]
        return result
    try:
        for threshold in (account_warning_fraction, reference_warning_fraction):
            if number(threshold, positive=True) > 1:
                raise ValueError("invalid_warning_fraction")
        positions = snapshot["data"]["POSITION"]
        matches = [row for row in positions if row["symbol"] == symbol]
        if len(matches) > 1:
            raise ValueError("duplicate_position")
        if not matches:
            # A complete stable POSITION table can establish zero holdings.
            value = 0.0
        else:
            row = matches[0]
            quantity = number(row["quantity"])
            value = quantity * number(row["mark_price"], positive=True) if quantity else 0.0
        number(value)
        result["account_symbol_value"] = value
        result["account_value_vs_strategy_reference"] = value / STRATEGY_REFERENCE
        if value >= STRATEGY_REFERENCE * reference_warning_fraction:
            result["alerts"].append("ACCOUNT_VALUE_VS_STRATEGY_REFERENCE_HIGH")
        try:
            equity = number(snapshot["data"]["ACCOUNT"]["total_equity"], positive=True)
        except (ValueError, TypeError, KeyError):
            result["alerts"].append("ACCOUNT_EQUITY_UNAVAILABLE_NOT_CLEARED")
            return result
        result["account_total_equity"] = equity
        result["account_equity_fraction"] = value / equity
        if value >= equity * account_warning_fraction:
            result["alerts"].append("ACCOUNT_SYMBOL_CONCENTRATION_HIGH")
        result["status"] = "warning" if result["alerts"] else "ok"
    except (ValueError, TypeError, KeyError):
        result["alerts"].append("EXPOSURE_INPUT_UNAVAILABLE_NOT_CLEARED")
    return result


def _ratio(value):
    return "未知" if value is None else format(value * 100, ".2f") + "%"


def _amount(value):
    return "未知" if value is None else format(value, ".2f") + "元"


def publish_exposure(report, output_dir):
    """Explicit independent directory only; never writes the strategy alert ledger."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    lines = ["账户同标暴露：独立只读告警", "不改变策略权限或21万元策略额度。",
             "核算时点：" + (report.get("as_of") or "未知，不能视为实时观测"),
             "账户同标市值：" + _amount(report["account_symbol_value"]),
             "分母一，账户总权益：" + _amount(report["account_total_equity"]),
             "账户集中度 = 账户同标市值 / 账户总权益：" + _ratio(report["account_equity_fraction"]),
             "分母二，策略本金参考：" + _amount(report["strategy_reference_equity"]),
             "参考比例 = 账户同标市值 / 策略30万元本金：" + _ratio(report["account_value_vs_strategy_reference"]),
             "参考比例不是策略实际仓位占比。", "数据状态：" + report["status"],
             "提示：" + (", ".join(report["alerts"]) or "当前有效快照未触发提示")]
    _write_atomic(root / "account_exposure.json", json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    _write_atomic(root / "ACCOUNT_EXPOSURE_READONLY.txt", "\n".join(lines) + "\n")


def monitor_bundle(bundle, *, symbol, now, output_dir=None, **limits):
    validation = validate_snapshot(bundle, now=now, **limits)
    report = account_exposure(validation, symbol)
    report["snapshot_validation"] = {key: deepcopy(value) for key, value in validation.items() if key != "data"}
    if output_dir is not None:
        publish_exposure(report, output_dir)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="两轮完整读取的标准化快照JSON")
    parser.add_argument("--out", type=Path, required=True, help="独立诊断目录，不使用策略告警目录")
    parser.add_argument("--symbol", default="300319.SZ")
    args = parser.parse_args(argv)
    try:
        bundle = json.loads(args.input.read_text(encoding="utf-8-sig"))
        report = monitor_bundle(bundle, symbol=args.symbol, now=datetime.now(timezone.utc).isoformat(), output_dir=args.out)
        print(json.dumps({"status": report["status"], "alerts": report["alerts"],
                          "affects_strategy_permissions": False}, ensure_ascii=False))
        # A warning is successful diagnostic output, not a strategy rejection.
        return 1 if report["status"] == "data_unavailable" else 0
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"status": "monitor_io_or_input_error", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
