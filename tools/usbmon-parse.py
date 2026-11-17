import sys
from collections import defaultdict
ev = defaultdict(lambda: dict(S=0, C=0, pk=0, urb_bad=0, errc=0, pst=defaultdict(int), ust=defaultdict(int), ts=[], lat=[], per=defaultdict(int), errper=defaultdict(int)))
sub = {}; t_first = None; bad_lines = 0
for line in open(sys.argv[1], errors="replace"):
    p = line.split()
    try:
        tag, ts, typ, addr = p[0], int(p[1]), p[2], p[3]
        a = addr.split(":"); key = addr[1] + ":ep" + a[3]
        st = p[4].split(":")
        if t_first is None: t_first = ts
        sec = (ts - t_first) // 1000000
        e = ev[key]
        if typ == "S":
            e["S"] += 1; sub[tag] = ts
        elif typ == "C":
            e["C"] += 1; e["ts"].append(ts); e["per"][sec] += 1
            status = int(st[0]); e["ust"][status] += 1
            if status != 0: e["urb_bad"] += 1
            errc = int(st[3]) if len(st) > 3 else 0
            e["errc"] += errc; e["errper"][sec] += errc
            nd = int(p[5]); e["pk"] += nd
            for d in p[6:6 + min(nd, 5)]:
                x = d.split(":")
                if len(x) == 3: e["pst"][int(x[0])] += 1
            if tag in sub: e["lat"].append(ts - sub.pop(tag))
    except Exception:
        bad_lines += 1
if not ev: print("no isochronous events for the Volt 2 were captured (is audio playing?)"); sys.exit(0)
dur = max(1, (max(max(e["ts"]) for e in ev.values() if e["ts"]) - t_first) / 1e6)
names = {"o": "playback data  (PC -> Volt)", "i": "feedback/capture (Volt -> PC)"}
for key in sorted(ev):
    e = ev[key]; ts = e["ts"]
    gaps = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1)) or [0]
    lat = sorted(e["lat"]) or [0]
    med = gaps[len(gaps) // 2]
    late = sum(1 for g in gaps if g > max(3 * med, med + 1000))
    print(f"\n{key}  {names.get(key[0], '')}")
    print(f"  URBs submitted={e['S']} completed={e['C']} ({e['C']/dur:.0f}/s)   packets={e['pk']} ({e['pk']/dur:.0f}/s)")
    print(f"  URBs completed with an error status: {e['urb_bad']}   statuses: {dict(e['ust'])}")
    print(f"  PACKETS REPORTED IN ERROR by the controller: {e['errc']}  ({e['errc']/dur:.2f}/s)")
    print(f"  per-packet statuses seen (first 5 packets of each URB): {dict(e['pst'])}   (-18 = missed its time slot)")
    print(f"  time between completions (us): median={med} p99={gaps[int(len(gaps)*0.99)]} max={gaps[-1]}   late completions (>3x median): {late}")
    print(f"  submit-to-complete latency (us): median={lat[len(lat)//2]} p99={lat[int(len(lat)*0.99)]} max={lat[-1]}")
    n = int(dur) + 1
    print("  packet errors per second:", " ".join(str(e["errper"].get(s, 0)) for s in range(n)))
if bad_lines: print(f"\n({bad_lines} lines could not be parsed)")
