from PIL import Image, ImageDraw
from pathlib import Path
import os
os.chdir(Path(__file__).resolve().parent.parent)
Path(".runtime").mkdir(exist_ok=True)
S=4; N=256
im=Image.new('RGBA',(N*S,N*S),(0,0,0,0))
mask=Image.new('L',im.size);d=ImageDraw.Draw(mask)
d.rounded_rectangle((14*S,14*S,242*S,242*S),radius=58*S,fill=255)
base=Image.new('RGBA',im.size)
p=base.load()
for y in range(N*S):
 for x in range(N*S):
  t=(.45*x+.55*y)/(N*S)
  p[x,y]=(int(47-23*t),int(143-47*t),int(248-19*t),255)
im.paste(base,(0,0),mask)
d=ImageDraw.Draw(im)
def box(rect,r,fill):d.rounded_rectangle(tuple(int(v*S) for v in rect),radius=r*S,fill=fill)
def poly(points,fill):d.polygon([(x*S,y*S) for x,y in points],fill=fill)
box((105,105,207,188),24,(181,224,255,255))
poly([(168,180),(190,204),(190,174)],(181,224,255,255))
box((49,56,178,151),28,(255,255,255,255))
poly([(69,137),(69,174),(107,143)],(255,255,255,255))
box((75,87,151,96),4,(58,128,221,255))
box((75,111,126,120),4,(109,165,236,255))
im=im.resize((256,256),Image.Resampling.LANCZOS)
im.save('.runtime/wechat-assistant-v2.png')
im.save('wechat-assistant-v2.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
# Show both artwork and its actual desktop size on a desktop-like background.
preview=Image.new('RGB',(460,300),(28,71,111))
preview.paste(im,(30,22),im)
small=im.resize((48,48),Image.Resampling.LANCZOS);preview.paste(small,(350,120),small)
preview.save('.runtime/icon-v2-preview.png')
print('ICO sizes:',sorted(Image.open('wechat-assistant-v2.ico').ico.sizes()))
