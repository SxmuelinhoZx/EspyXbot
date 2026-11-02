"""Module for Exnova websocket."""

def time_sync(api, message):
    if message["name"] == "timeSync":
        import time as _t
        now = _t.time()
        api.timesync.server_timestamp = message["msg"]
        api.timesync_recv = now
        try:
            api.timesync_samples.append((float(message["msg"]) / 1000.0, now))
        except Exception:
            pass