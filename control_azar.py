import sys, time
sys.path.insert(0, "espyx_bot_v15_2")
import strategy as st
c = st.random_walk(20000, seed=7)
t = time.time()
res = st.compare(c, minutes=[1,3,5], payout=0.85, keys=["structure","retrace","regime","pullback","reversion","trend"])
print("seg:", round(time.time()-t))
for it in res:
    v = it["valid"]
    print(it["key"], it.get("minutes"), {k: v[k] for k in v if k in ("n","wins","wr","lo","trades","win_rate")} if isinstance(v, dict) else v)
