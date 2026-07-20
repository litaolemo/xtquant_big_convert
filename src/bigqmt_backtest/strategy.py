"""Small external-strategy contract and a synchronous ZMQ runner."""


class StrategyContext(object):
    def __init__(self, client):
        self.client = client
        self.state = None

    @property
    def now(self):
        return None if self.state is None else self.state.get("datetime")

    @property
    def cash(self):
        return 0 if self.state is None else self.state.get("cash", 0)

    @property
    def positions(self):
        return {} if self.state is None else self.state.get("positions", {})

    def history(self, symbol, count=100, fields=None):
        return self.client.history(symbol, count=count, fields=fields)


class ExternalStrategyRunner(object):
    """Drive a user strategy without exposing future bars.

    Strategy methods are optional:

    * ``on_start(context)``
    * ``on_bar(context, bars) -> iterable[order dict]``
    * ``on_fill(context, fill)``
    * ``on_finish(context, result)``
    """

    def __init__(self, client, strategy):
        self.client = client
        self.strategy = strategy
        self.context = StrategyContext(client)

    def _call(self, name, *args):
        callback = getattr(self.strategy, name, None)
        return callback(*args) if callback is not None else None

    def _apply_orders(self, orders):
        for order in list(orders or []):
            payload = dict(order)
            self.client.submit_order(
                symbol=payload["symbol"],
                side=payload["side"],
                quantity=payload["quantity"],
                order_type=payload.get("order_type", "MARKET"),
                limit_price=payload.get("limit_price"),
                client_order_id=payload.get("client_order_id", ""),
                time_in_force=payload.get("time_in_force", "NEXT_BAR"),
            )

    def run(self):
        if not self.client.run_id:
            self.client.describe()
        state = self.client.start()
        self.context.state = state
        self._call("on_start", self.context)
        while True:
            for fill in state.get("fills", []):
                self._call("on_fill", self.context, fill)
            orders = self._call("on_bar", self.context, state.get("bars", {}))
            self._apply_orders(orders)
            if state.get("done"):
                break
            state = self.client.next_bar()
            self.context.state = state
        result = self.client.finish()
        self._call("on_finish", self.context, result)
        return result
