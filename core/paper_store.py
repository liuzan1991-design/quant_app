# -*- coding: utf-8 -*-
"""模拟盘状态持久化：JSON 快照 + JSONL 追加式审计日志。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import (
    AccountState,
    Fill,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from core.risk_manager import RiskConfig, RiskManager


class PaperStateStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.root / "paper_state.json"
        self.audit_path = self.root / "paper_audit.jsonl"

    @staticmethod
    def _json_default(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        raise TypeError(f"不支持序列化：{type(value).__name__}")

    def append_audit(self, event: dict) -> None:
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False,
                                    default=self._json_default, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def flush_new_audit(self, broker: PaperBroker) -> int:
        cursor = getattr(broker, "_persisted_audit_count", 0)
        events = broker.audit_log[cursor:]
        for event in events:
            self.append_audit(event)
        broker._persisted_audit_count = len(broker.audit_log)
        return len(events)

    def save(self, broker: PaperBroker) -> Path:
        self.flush_new_audit(broker)
        payload = {
            "version": 1,
            "saved_at": datetime.now().isoformat(),
            "broker_config": asdict(broker.config),
            "risk_config": asdict(broker.risk_manager.config),
            "account": asdict(broker.account),
            "orders": [asdict(item) for item in broker.orders.values()],
            "fills": [asdict(item) for item in broker.fills],
        }
        temp = self.snapshot_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False,
                                   default=self._json_default, indent=2), encoding="utf-8")
        os.replace(temp, self.snapshot_path)
        return self.snapshot_path

    def load(self) -> Optional[PaperBroker]:
        if not self.snapshot_path.exists():
            return None
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        risk = RiskManager(RiskConfig(**payload["risk_config"]))
        broker = PaperBroker(
            initial_cash=float(payload["account"]["initial_cash"]),
            trading_day=payload["account"]["trading_day"],
            risk_manager=risk,
            config=PaperBrokerConfig(**payload["broker_config"]),
        )
        account_data = payload["account"]
        positions = {
            symbol: Position(**position)
            for symbol, position in account_data.get("positions", {}).items()
        }
        broker.account = AccountState(
            initial_cash=float(account_data["initial_cash"]),
            cash=float(account_data["cash"]),
            trading_day=account_data["trading_day"],
            positions=positions,
            realized_pnl_by_symbol=dict(account_data.get("realized_pnl_by_symbol", {})),
            daily_realized_pnl=float(account_data.get("daily_realized_pnl", 0.0)),
            consecutive_stop_losses=dict(account_data.get("consecutive_stop_losses", {})),
            symbol_pause_until=dict(account_data.get("symbol_pause_until", {})),
        )
        for item in payload.get("orders", []):
            request_data = item["request"]
            request = OrderRequest(
                client_order_id=request_data["client_order_id"],
                symbol=request_data["symbol"],
                side=OrderSide(request_data["side"]),
                quantity=int(request_data["quantity"]),
                created_at=datetime.fromisoformat(request_data["created_at"]),
                order_type=OrderType(request_data["order_type"]),
                limit_price=request_data.get("limit_price"),
                strategy_id=request_data.get("strategy_id", ""),
            )
            order = Order(
                request=request,
                status=OrderStatus(item["status"]),
                filled_quantity=int(item.get("filled_quantity", 0)),
                average_fill_price=float(item.get("average_fill_price", 0.0)),
                reject_reason=item.get("reject_reason", ""),
                updated_at=(datetime.fromisoformat(item["updated_at"])
                            if item.get("updated_at") else None),
            )
            broker.orders[request.client_order_id] = order
        broker.fills = [
            Fill(
                fill_id=item["fill_id"], client_order_id=item["client_order_id"],
                symbol=item["symbol"], side=OrderSide(item["side"]),
                quantity=int(item["quantity"]), price=float(item["price"]),
                fee=float(item["fee"]), filled_at=datetime.fromisoformat(item["filled_at"]),
            )
            for item in payload.get("fills", [])
        ]
        broker._persisted_audit_count = 0
        return broker
