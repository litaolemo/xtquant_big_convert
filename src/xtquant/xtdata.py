import bigqmt_signal_trader.xtquant_compat as _compat


def get_full_tick(code_list):
    return _compat.xtdata.get_full_tick(code_list)


def get_instrument_detail(stock_code):
    return _compat.xtdata.get_instrument_detail(stock_code)


def get_instrumentdetail(stock_code):
    return _compat.xtdata.get_instrumentdetail(stock_code)


def get_stock_list_in_sector(sector_name):
    return _compat.xtdata.get_stock_list_in_sector(sector_name)


def subscribe_whole_quote(code_list, callback=None):
    return _compat.xtdata.subscribe_whole_quote(code_list, callback=callback)


def unsubscribe_quote(*args, **kwargs):
    return _compat.xtdata.unsubscribe_quote(*args, **kwargs)
