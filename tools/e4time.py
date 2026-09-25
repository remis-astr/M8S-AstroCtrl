import serial, time, datetime as dt, re
s = serial.Serial("/dev/ttyUSB0", 9600, timeout=0.3)
def q(c):
    s.reset_input_buffer(); s.write(c.encode()); b=b""
    t=time.time()
    while time.time()-t<1:
        x=s.read(1)
        if x==b"#": break
        b+=x
    return b.decode()
time.sleep(0.3)
now=dt.datetime.now(dt.timezone.utc)
gs,gc,gl,gg,gg2=q(":GSH#"),q(":GC#"),q(":GLH#"),q(":GG#"),q(":GgH#")
print("Reel : UTC", now.strftime("%H:%M:%S"), "/ Paris", now.astimezone().strftime("%H:%M:%S %Z"))
print("E4   : date",gc," locale",gl," :GG",gg," TSL",gs)
m=re.match(r"([+-])(\d+)\D(\d+)\D?([\d.]*)",gg2); lon=(int(m[2])+int(m[3])/60+(float(m[4]) if m[4] else 0)/3600)*(-1 if m[1]=="+" else 1)
d=now.timestamp()/86400+2440587.5-2451545.0
lst=((18.697374558+24.06570982441908*d)%24+lon/15)%24
h=[float(x) for x in gs.split(":")]; e4=h[0]+h[1]/60+h[2]/3600
mo,da,yy=map(int,gc.split("/")); hh,mi,ss=gl.split(":")
loc=dt.datetime(2000+yy,mo,da,int(hh),int(mi),0,tzinfo=dt.timezone.utc)+dt.timedelta(seconds=float(ss))
sg=-1 if gg[0]=="-" else 1; oh,om=gg[1:].split(":")
utc=loc+sg*dt.timedelta(hours=int(oh),minutes=int(om))
print("Ecart UTC = %.2f s   ecart TSL = %.2f s"%((utc-now).total_seconds(),((e4-lst+12)%24-12)*3600))
