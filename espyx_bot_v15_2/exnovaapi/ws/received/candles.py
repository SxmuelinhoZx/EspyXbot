"""Module for Exnova websocket."""

def candles(api, message):
    if message['name'] == 'candles':
        try:
            data = message["msg"]["candles"]
        except Exception:
            return
        rid = message.get("request_id")
        if rid not in (None, ""):
            store = api.candles_by_req
            if len(store) > 200:          # nunca crece sin límite (respuestas que nadie reclamó)
                store.clear()
            store[str(rid)] = data
        api.candles.candles_data = data