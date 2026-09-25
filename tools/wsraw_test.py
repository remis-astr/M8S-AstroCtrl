import asyncio, json, struct, time, sys, websockets
async def main(n, fps):
    async with websockets.connect("ws://192.168.7.3:8000/ws/raw", max_size=None) as ws:
        await ws.send(json.dumps({"cmd":"set_rate","fps":fps}))
        t0=time.monotonic(); last=t0
        for i in range(n):
            m = await ws.recv()
            if isinstance(m,str): print("txt",m); continue
            L=struct.unpack(">I",m[:4])[0]; meta=json.loads(m[4:4+L]); now=time.monotonic()
            print("frame %d: %dx%d bits=%s bayer=%s exp=%sms payload=%.0fkB dt=%.2fs"%(i,meta["width"],meta["height"],meta["bit_depth"],meta["bayer"],meta["exposure_ms"],(len(m)-4-L)/1024,now-last)); last=now
        await ws.send(json.dumps({"cmd":"stop"}))
asyncio.run(main(int(sys.argv[1]), float(sys.argv[2])))
