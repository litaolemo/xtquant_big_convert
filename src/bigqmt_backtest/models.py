"""Backtest-only domain models with JSON-safe serialization."""

from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.01")


def decimal_value(value, default="0"):
    if value in (None, ""):
        value = default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value):
    return decimal_value(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def json_number(value, digits=None):
    if value is None:
        return None
    number = float(value)
    return round(number, digits) if digits is not None else number


def normalize_symbol(value):
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("symbol is required")
    if "." in text:
        pure, market = text.rsplit(".", 1)
        if pure and market in ("SH", "SZ", "BJ"):
            return "%s.%s" % (pure, market)
        return text
    if text.isdigit() and len(text) == 6:
        if text.startswith(("4", "8")):
            return text + ".BJ"
        if text.startswith(("5", "6", "9")):
            return text + ".SH"
        return text + ".SZ"
    return text


def price_precision(symbol):
    pure = normalize_symbol(symbol).split(".", 1)[0]
    return 3 if pure.startswith(("15", "16", "50", "51", "52", "56", "58")) else 2


def price_quant(symbol):
    return Decimal("0.001") if price_precision(symbol) == 3 else Decimal("0.01")


def round_price(symbol, value):
    return decimal_value(value).quantize(price_quant(symbol), rounding=ROUND_HALF_UP)


class Position(object):
    def __init__(self, symbol, quantity=0, available=None, today_buy=0, avg_cost=0, realized_pnl=0):
        self.symbol = normalize_symbol(symbol)
        self.quantity = int(quantity or 0)
        self.available = self.quantity if available is None else int(available or 0)
        self.today_buy = int(today_buy or 0)
        self.avg_cost = decimal_value(avg_cost)
        self.realized_pnl = decimal_value(realized_pnl)

    def to_dict(self, mark_price=None):
        market_value = None if mark_price is None else money(decimal_value(mark_price) * self.quantity)
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "available": self.available,
            "today_buy": self.today_buy,
            "avg_cost": json_number(self.avg_cost, 6),
            "realized_pnl": json_number(self.realized_pnl, 2),
            "mark_price": json_number(mark_price, 6),
            "market_value": json_number(market_value, 2),
        }


class BacktestOrder(object):
    def __init__(
        self,
        order_id,
        client_order_id,
        symbol,
        side,
        quantity,
        order_type,
        limit_price,
        submitted_index,
        submitted_at,
        time_in_force="NEXT_BAR",
    ):
        self.order_id = str(order_id)
        self.client_order_id = str(client_order_id or "")
        self.symbol = normalize_symbol(symbol)
        self.side = str(side).upper()
        self.quantity = int(quantity)
        self.filled_quantity = 0
        self.order_type = str(order_type).upper()
        self.limit_price = decimal_value(limit_price) if limit_price not in (None, "") else None
        self.submitted_index = int(submitted_index)
        self.submitted_at = str(submitted_at)
        self.time_in_force = str(time_in_force or "NEXT_BAR").upper()
        self.status = "PENDING"
        self.reject_reason = ""
        self.last_attempt_index = None

    @property
    def remaining(self):
        return max(self.quantity - self.filled_quantity, 0)

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining,
            "order_type": self.order_type,
            "limit_price": json_number(self.limit_price, 6),
            "submitted_index": self.submitted_index,
            "submitted_at": self.submitted_at,
            "time_in_force": self.time_in_force,
            "status": self.status,
            "reject_reason": self.reject_reason,
        }


class BacktestFill(object):
    def __init__(
        self,
        fill_id,
        order,
        quantity,
        price,
        commission,
        stamp_tax,
        transfer_fee,
        filled_index,
        filled_at,
    ):
        self.fill_id = str(fill_id)
        self.order_id = order.order_id
        self.client_order_id = order.client_order_id
        self.symbol = order.symbol
        self.side = order.side
        self.quantity = int(quantity)
        self.price = decimal_value(price)
        self.amount = money(self.price * self.quantity)
        self.commission = money(commission)
        self.stamp_tax = money(stamp_tax)
        self.transfer_fee = money(transfer_fee)
        self.total_fee = money(self.commission + self.stamp_tax + self.transfer_fee)
        self.filled_index = int(filled_index)
        self.filled_at = str(filled_at)

    def to_dict(self):
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": json_number(self.price, 6),
            "amount": json_number(self.amount, 2),
            "commission": json_number(self.commission, 2),
            "stamp_tax": json_number(self.stamp_tax, 2),
            "transfer_fee": json_number(self.transfer_fee, 2),
            "total_fee": json_number(self.total_fee, 2),
            "filled_index": self.filled_index,
            "filled_at": self.filled_at,
        }
