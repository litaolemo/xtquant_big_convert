"""A-share simulated broker used only by the standalone backtest runtime."""

import json
from decimal import Decimal

from .models import (
    BacktestFill,
    BacktestOrder,
    Position,
    ZERO,
    decimal_value,
    json_number,
    money,
    normalize_symbol,
    round_price,
)


ACTIVE_ORDER_STATUSES = ("PENDING", "PARTIALLY_FILLED")


class SimulatedBroker(object):
    def __init__(self, config):
        self.config = config
        self.cash = money(config.initial_cash)
        self.positions = {}
        for symbol, payload in dict(config.initial_positions or {}).items():
            data = dict(payload or {})
            position = Position(
                symbol,
                quantity=data.get("quantity", data.get("volume", 0)),
                available=data.get("available"),
                today_buy=data.get("today_buy", 0),
                avg_cost=data.get("avg_cost", data.get("cost", 0)),
            )
            if position.quantity > 0:
                self.positions[position.symbol] = position
        self.orders_list = []
        self.fills_list = []
        self._client_order_ids = {}
        self._client_order_fingerprints = {}
        self._order_sequence = 0
        self._fill_sequence = 0
        self._trading_date = None
        self.total_fees = ZERO
        self.turnover = ZERO

    def _new_order(self, payload, frame_index, submitted_at):
        self._order_sequence += 1
        return BacktestOrder(
            order_id="bt-order-%06d" % self._order_sequence,
            client_order_id=payload.get("client_order_id"),
            symbol=payload.get("symbol"),
            side=payload.get("side"),
            quantity=payload.get("quantity"),
            order_type=payload.get("order_type", "MARKET"),
            limit_price=payload.get("limit_price", payload.get("price")),
            submitted_index=frame_index,
            submitted_at=submitted_at,
            time_in_force=payload.get("time_in_force", self.config.time_in_force),
        )

    def _reject(self, order, reason):
        order.status = "REJECTED"
        order.reject_reason = str(reason)
        return order

    def _reserved_sell(self, symbol):
        return sum(
            order.remaining
            for order in self.orders_list
            if order.symbol == symbol and order.side == "SELL" and order.status in ACTIVE_ORDER_STATUSES
        )

    def submit(self, payload, frame_index, submitted_at):
        payload = dict(payload or {})
        client_order_id = str(payload.get("client_order_id") or "")
        if client_order_id and client_order_id in self._client_order_ids:
            limit_value = payload.get("limit_price", payload.get("price"))
            fingerprint_payload = {
                "symbol": normalize_symbol(payload.get("symbol")),
                "side": str(payload.get("side") or "").upper(),
                "quantity": int(payload.get("quantity") or 0),
                "order_type": str(payload.get("order_type") or "MARKET").upper(),
                "limit_price": None if limit_value in (None, "") else float(decimal_value(limit_value)),
                "time_in_force": str(payload.get("time_in_force", self.config.time_in_force)).upper(),
            }
            fingerprint = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
            if self._client_order_fingerprints.get(client_order_id) != fingerprint:
                raise ValueError("client_order_id reused with different order payload")
            return self._client_order_ids[client_order_id]
        try:
            symbol = normalize_symbol(payload.get("symbol"))
            side = str(payload.get("side") or "").upper()
            quantity = int(payload.get("quantity") or 0)
            order_type = str(payload.get("order_type") or "MARKET").upper()
            if side not in ("BUY", "SELL"):
                raise ValueError("side must be BUY or SELL")
            if quantity <= 0:
                raise ValueError("quantity must be positive")
            if order_type not in ("MARKET", "LIMIT"):
                raise ValueError("order_type must be MARKET or LIMIT")
            if order_type == "LIMIT" and decimal_value(payload.get("limit_price", payload.get("price"))) <= 0:
                raise ValueError("positive limit_price is required for LIMIT order")
            payload.update({"symbol": symbol, "side": side, "quantity": quantity, "order_type": order_type})
            order = self._new_order(payload, frame_index, submitted_at)
        except Exception as exc:
            self._order_sequence += 1
            order = BacktestOrder(
                "bt-order-%06d" % self._order_sequence,
                client_order_id,
                payload.get("symbol") or "UNKNOWN",
                payload.get("side") or "UNKNOWN",
                int(payload.get("quantity") or 0),
                payload.get("order_type") or "MARKET",
                payload.get("limit_price", payload.get("price")),
                frame_index,
                submitted_at,
                payload.get("time_in_force", self.config.time_in_force),
            )
            self._reject(order, "invalid_order:%s" % exc)
            self.orders_list.append(order)
            return order

        self.orders_list.append(order)
        if client_order_id:
            self._client_order_ids[client_order_id] = order
            fingerprint_payload = {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "order_type": order.order_type,
                "limit_price": None if order.limit_price is None else float(order.limit_price),
                "time_in_force": order.time_in_force,
            }
            self._client_order_fingerprints[client_order_id] = json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            )
        lot_size = self.config.lot_size
        if order.side == "BUY" and order.quantity % lot_size:
            return self._reject(order, "buy_quantity_not_round_lot")
        if order.side == "SELL":
            position = self.positions.get(order.symbol)
            available = 0 if position is None else max(position.available - self._reserved_sell(order.symbol) + order.quantity, 0)
            if available <= 0:
                return self._reject(order, "t_plus_one_unavailable")
            if order.quantity > available:
                return self._reject(order, "insufficient_sellable")
            if order.quantity % lot_size and order.quantity != available:
                return self._reject(order, "sell_quantity_not_round_lot")
        return order

    def cancel(self, order_id):
        for order in self.orders_list:
            if order.order_id == str(order_id) or order.client_order_id == str(order_id):
                if order.status not in ACTIVE_ORDER_STATUSES:
                    return order
                order.status = "CANCELLED"
                order.reject_reason = "cancelled_by_strategy"
                return order
        raise KeyError("order not found: %s" % order_id)

    def _settle_trading_day(self, trading_date):
        if self._trading_date == trading_date:
            return
        if self._trading_date is not None:
            for position in self.positions.values():
                position.available = position.quantity
                position.today_buy = 0
        self._trading_date = trading_date

    def _limits(self, order, bar):
        prev_close = decimal_value(bar.get("prev_close") or bar.get("close"))
        if bar.get("price_limit_rate") not in (None, ""):
            rate = decimal_value(bar.get("price_limit_rate"))
        else:
            pure = order.symbol.split(".", 1)[0]
            if order.symbol.endswith(".BJ"):
                rate = Decimal("0.30")
            elif pure.startswith(("300", "301", "688", "689")):
                rate = Decimal("0.20")
            else:
                rate = decimal_value(self.config.price_limit_rate)
        up_limit = bar.get("up_limit")
        down_limit = bar.get("down_limit")
        up_limit = round_price(order.symbol, up_limit if up_limit not in (None, "") else prev_close * (Decimal("1") + rate))
        down_limit = round_price(order.symbol, down_limit if down_limit not in (None, "") else prev_close * (Decimal("1") - rate))
        return up_limit, down_limit

    def _match_price(self, order, bar):
        if bool(bar.get("suspended")) or float(bar.get("volume") or 0) <= 0:
            return None, "suspended_or_no_volume"
        open_price = round_price(order.symbol, bar["open"])
        high = round_price(order.symbol, bar["high"])
        low = round_price(order.symbol, bar["low"])
        up_limit, down_limit = self._limits(order, bar)
        if order.side == "BUY" and open_price == high == low == up_limit:
            return None, "limit_up_locked"
        if order.side == "SELL" and open_price == high == low == down_limit:
            return None, "limit_down_locked"
        if order.order_type == "LIMIT" and not down_limit <= order.limit_price <= up_limit:
            return None, "limit_price_outside_daily_range"
        if order.order_type == "MARKET":
            price = open_price
        elif order.side == "BUY":
            if low > order.limit_price:
                return None, "limit_not_crossed"
            price = min(open_price, order.limit_price)
        else:
            if high < order.limit_price:
                return None, "limit_not_crossed"
            price = max(open_price, order.limit_price)
        slip = decimal_value(self.config.slippage_bps) / Decimal("10000")
        if order.side == "BUY":
            price = min(round_price(order.symbol, price * (Decimal("1") + slip)), up_limit)
        else:
            price = max(round_price(order.symbol, price * (Decimal("1") - slip)), down_limit)
        return price, ""

    def _fees(self, side, amount):
        rate = self.config.buy_commission_rate if side == "BUY" else self.config.sell_commission_rate
        commission = max(amount * decimal_value(rate), decimal_value(self.config.min_commission)) if rate else ZERO
        stamp = amount * decimal_value(self.config.stamp_tax_rate) if side == "SELL" else ZERO
        transfer = amount * decimal_value(self.config.transfer_fee_rate)
        return money(commission), money(stamp), money(transfer)

    def _volume_cap(self, order, bar, used_volume=0):
        raw = int(float(bar.get("volume") or 0) * float(self.config.max_volume_participation))
        cap = max((raw // self.config.lot_size) * self.config.lot_size - int(used_volume), 0)
        return min(order.remaining, cap)

    def _affordable_buy_quantity(self, quantity, price):
        quantity = (int(quantity) // self.config.lot_size) * self.config.lot_size
        while quantity > 0:
            amount = money(price * quantity)
            fees = sum(self._fees("BUY", amount), ZERO)
            if self.cash >= amount + fees:
                return quantity
            quantity -= self.config.lot_size
        return 0

    def _apply_fill(self, order, quantity, price, frame_index, filled_at):
        amount = money(price * quantity)
        commission, stamp, transfer = self._fees(order.side, amount)
        self._fill_sequence += 1
        fill = BacktestFill(
            "bt-fill-%06d" % self._fill_sequence,
            order,
            quantity,
            price,
            commission,
            stamp,
            transfer,
            frame_index,
            filled_at,
        )
        fees = fill.total_fee
        position = self.positions.get(order.symbol)
        if order.side == "BUY":
            if position is None:
                position = Position(order.symbol)
                self.positions[order.symbol] = position
            old_cost = position.avg_cost * position.quantity
            self.cash = money(self.cash - amount - fees)
            position.quantity += quantity
            position.today_buy += quantity
            position.avg_cost = (old_cost + amount + fees) / position.quantity
        else:
            if position is None or position.available < quantity:
                raise RuntimeError("sellable quantity changed before fill")
            self.cash = money(self.cash + amount - fees)
            position.quantity -= quantity
            position.available -= quantity
            position.realized_pnl += amount - fees - position.avg_cost * quantity
            if position.quantity <= 0:
                self.positions.pop(order.symbol, None)
        order.filled_quantity += quantity
        order.status = "FILLED" if order.remaining == 0 else "PARTIALLY_FILLED"
        self.total_fees += fees
        self.turnover += amount
        self.fills_list.append(fill)
        return fill

    def advance(self, frame_index, frame):
        trading_date = str(frame["datetime"])[:10]
        self._settle_trading_day(trading_date)
        fills = []
        used_volume = {}
        for order in self.orders_list:
            if order.status not in ACTIVE_ORDER_STATUSES or frame_index <= order.submitted_index:
                continue
            bar = frame["bars"].get(order.symbol)
            if bar is None:
                continue
            if order.time_in_force == "DAY" and str(order.submitted_at)[:10] != trading_date:
                order.status = "EXPIRED"
                order.reject_reason = "day_order_expired"
                continue
            order.last_attempt_index = frame_index
            price, reason = self._match_price(order, bar)
            if price is None:
                if order.time_in_force == "NEXT_BAR":
                    order.status = "EXPIRED"
                    order.reject_reason = reason
                continue
            quantity = self._volume_cap(order, bar, used_volume.get(order.symbol, 0))
            if quantity <= 0:
                reason = "volume_participation_exhausted"
            elif order.side == "BUY":
                quantity = self._affordable_buy_quantity(quantity, price)
                if quantity <= 0:
                    reason = "insufficient_cash"
            else:
                position = self.positions.get(order.symbol)
                quantity = min(quantity, 0 if position is None else position.available)
                if quantity <= 0:
                    reason = "t_plus_one_unavailable"
            if quantity > 0:
                fills.append(self._apply_fill(order, quantity, price, frame_index, frame["datetime"]))
                used_volume[order.symbol] = used_volume.get(order.symbol, 0) + quantity
            if order.time_in_force == "NEXT_BAR" and order.remaining > 0:
                if order.filled_quantity == 0:
                    order.status = "EXPIRED"
                else:
                    order.status = "PARTIALLY_FILLED_EXPIRED"
                order.reject_reason = reason or "next_bar_remaining_expired"
        return fills

    def expire_open_orders(self, reason="backtest_finished"):
        for order in self.orders_list:
            if order.status in ACTIVE_ORDER_STATUSES:
                order.status = "EXPIRED"
                order.reject_reason = reason

    def snapshot(self, bars):
        positions = {}
        market_value = ZERO
        for symbol in sorted(self.positions):
            position = self.positions[symbol]
            bar = bars.get(symbol) or {}
            mark = decimal_value(bar.get("close"), position.avg_cost)
            market_value += mark * position.quantity
            positions[symbol] = position.to_dict(mark)
        total_asset = money(self.cash + market_value)
        return {
            "cash": json_number(self.cash, 2),
            "market_value": json_number(money(market_value), 2),
            "total_asset": json_number(total_asset, 2),
            "positions": positions,
            "total_fees": json_number(money(self.total_fees), 2),
            "turnover": json_number(money(self.turnover), 2),
        }

    def orders(self):
        return [order.to_dict() for order in self.orders_list]

    def fills(self):
        return [fill.to_dict() for fill in self.fills_list]
