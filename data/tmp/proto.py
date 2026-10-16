import cv2, numpy as np, sys
c = cv2.VideoCapture('data/footage/CYF 15-5.mp4')
step = 10  # 3 fps
T_CHANGE, T_STABLE, K = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
ref = prev = None; stable = 0; events = []; i = 0; diffs = []
while True:
    ok, f = c.read()
    if not ok: break
    if i % step == 0:
        g = cv2.cvtColor(f[8:72, 150:258], cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            d = (np.abs(g - prev) > 40).sum(); diffs.append(d)
            stable = stable + 1 if d < T_STABLE else 0
            if ref is None and stable >= K: ref = g
            elif ref is not None and stable >= K and (np.abs(g - ref) > 40).sum() > T_CHANGE:
                events.append(round(i / 30, 1)); ref = g
        prev = g
    i += 1
print(len(events), events)
print('diff pctiles 50/90/99', np.percentile(diffs, [50, 90, 99]).round(2))
tiles = []
for t in events:
    c.set(0, (t + 0.4) * 1000); ok, f = c.read(); cr = f[0:80, 0:262].copy()
    cv2.putText(cr, f'{t:.0f}', (5, 76), 0, 0.45, (0, 255, 255), 1); tiles.append(cr)
while len(tiles) % 6: tiles.append(np.zeros_like(tiles[0]))
cv2.imwrite('data/tmp/events.jpg', np.vstack([np.hstack(tiles[i:i+6]) for i in range(0, len(tiles), 6)]))
