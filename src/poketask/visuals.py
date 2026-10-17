"""Pixel menu assets and original Metagross animation choreography."""
import math
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageSequence
from .paths import assets_dir

INK = "#243b43"
PAPER = "#eef2dc"
MINT = "#b8d0be"
GREEN = "#427b68"
RED = "#c95c58"
GOLD = "#d8ad57"
TRANSPARENT_KEY = "#ff00ff"
PET_WIDTH, PET_HEIGHT = 208, 170
METEOR_TIMING = {
    "charge": (0, .8), "windup": (.8, 1.92), "punch": (1.92, 2.08),
    "impact": (2.08, 2.40), "recover": (2.40, 3.10), "rest": (3.10, 3.80),
}
METEOR_DURATION = METEOR_TIMING["rest"][1]
STATES = {
    "idle": ("空闲", GREEN),
    "working": ("任务中", "#609da8"),
    "asking": ("询问状态", GOLD),
    "completed": ("本轮完成", GREEN),
    "waiting": ("等待中", GOLD),
    "failed": ("任务失败", RED),
    "interrupted": ("已中断", GOLD),
    "disconnected": ("未连接", "#73878b"),
    "unknown": ("状态待确认", "#73878b"),
}


def _ease(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


class PixelArt:
    def __init__(self):
        directory = assets_dir()
        self.font = ImageFont.truetype(str(directory / "pixel.ttf"), 12)
        self.frames = {}
        for name in ("metagross", "metagross-back"):
            with Image.open(directory / (name + ".gif")) as image:
                self.frames[name] = [f.convert("RGBA").copy() for f in ImageSequence.Iterator(image)]
        self.items = {}
        for name in ("poke-ball", "potion", "rare-candy", "air-balloon", "poke-flute",
                     "coin-case", "poke-radar", "town-map", "vs-recorder", "silph-scope", "escape-rope"):
            with Image.open(directory / (name + ".png")) as source:
                self.items[name] = source.convert("RGBA")
        self.unown = {}
        for mark, name in (("?", "question"), ("!", "exclamation")):
            with Image.open(directory / ("unown-" + name + ".png")) as source:
                sprite = source.convert("RGBA")
                self.unown[mark] = sprite.crop(sprite.getbbox())
        # Separate the official sprite's near foreleg and pivot its original
        # pixels around the shoulder. The Pokémon is not AI-generated/redrawn.
        original = self.frames["metagross"][0]
        mask = Image.new("L", original.size)
        ImageDraw.Draw(mask).polygon(((64,20),(83,20),(83,58),(62,58),
                                      (59,49),(60,36),(62,27)), fill=255)
        self.arm = original.copy()
        self.arm.putalpha(ImageChops.multiply(original.getchannel("A"), mask))
        self.body = original.copy()
        self.body.putalpha(ImageChops.multiply(original.getchannel("A"), ImageChops.invert(mask)))
        # Complete the occluded far leg from the official foreleg's plated
        # lower section and claws. It is smaller in perspective, shaded using
        # the source palette, and sits behind the body and animated near arm.
        lower_leg=original.crop((61,27,83,58)).resize((18,25),Image.Resampling.NEAREST)
        palette={pixel[:3] for pixel in original.getdata() if pixel[3]}
        shades={color:min(palette,key=lambda candidate:sum((candidate[i]-color[i]*.82)**2
                       for i in range(3))) for color in palette}
        lower_leg.putdata([(*shades[pixel[:3]],pixel[3]) if pixel[3] else pixel
                           for pixel in lower_leg.getdata()])
        self.rear_leg=Image.new("RGBA",original.size)
        self.rear_leg.alpha_composite(lower_leg,(60,21))
        # Keep the resting silhouette exactly inside the official source.
        self.rear_leg.putalpha(ImageChops.multiply(self.rear_leg.getchannel("A"),original.getchannel("A")))
        self.arm_rotations = {}
        self.arm_ghosts = {}
        self.limbs = {}
        for reach in range(8,13):
            stretch=reach/8
            arm=self.arm.resize((self.arm.width,round(self.arm.height*stretch)),Image.Resampling.NEAREST)
            limb=Image.new("RGBA",(128,128))
            limb.alpha_composite(arm,(0,64-round(25*stretch)))
            self.limbs[reach]=limb
        # Opaque pixel dithering also fades correctly on Tk's color-key window.
        # Translucent pixels would mix with the magenta transparency key.
        bayer=((0,8,2,10),(12,4,14,6),(3,11,1,9),(15,7,13,5))
        self.ghost_masks={}
        for coverage in (8,):
            pattern=Image.new("L",(128,128))
            pattern.putdata([255 if bayer[y%4][x%4]<coverage else 0
                             for y in range(128) for x in range(128)])
            self.ghost_masks[coverage]=pattern

    def text(self, image, xy, text, fill=INK):
        ImageDraw.Draw(image).text(xy,str(text),font=self.font,fill=fill,spacing=4)

    def fit(self,value,width):
        original=str(value)
        text=original[:max(1,int(width))]
        while text and self.font.getlength(text)>width:
            text=text[:-1]
        return text if text==original else text[:-1]+"…"

    def item(self,image,name,x,y,scale=1):
        """Inventory artwork is only used for menu controls, never pet actions."""
        sprite=self.items[name]
        if scale!=1:
            sprite=sprite.resize((sprite.width*scale,sprite.height*scale),Image.Resampling.NEAREST)
        image.alpha_composite(sprite,(int(x),int(y)))

    @staticmethod
    def meteor_phase(seconds):
        phase=seconds%METEOR_DURATION
        for name,(start,end) in METEOR_TIMING.items():
            if phase<end: return name,(phase-start)/(end-start)

    @staticmethod
    def _star(draw,cx,cy,radius,color):
        r=max(2,int(radius))
        cx,cy=int(cx),int(cy)
        draw.polygon(((cx,cy-r),(cx+2,cy-2),(cx+r,cy),(cx+2,cy+2),
                      (cx,cy+r),(cx-2,cy+2),(cx-r,cy),(cx-2,cy-2)),fill=color)

    def _arm_sprite(self,angle,reach=1):
        key=(round(angle/5)*5%360,max(8,min(12,round(reach*8))))
        if key not in self.arm_rotations:
            self.arm_rotations[key]=self.limbs[key[1]].rotate(key[0],Image.Resampling.NEAREST,center=(64,64))
        return key,self.arm_rotations[key]

    @staticmethod
    def _fist(pivot_x,pivot_y,angle,reach):
        radians=math.radians(round(angle/5)*5)
        length=27*max(8,min(12,round(reach*8)))/8
        return (int(pivot_x+6*math.cos(radians)+length*math.sin(radians)),
                int(pivot_y-6*math.sin(radians)+length*math.cos(radians)))

    def _body_pose(self,image,cx,cy,shoulder=0):
        x,y=int(cx-self.body.width//2),int(cy-self.body.height//2)
        image.alpha_composite(self.rear_leg,(x,y))
        image.alpha_composite(self.body,(x,y))
        anchor_x,pivot_y=x+64,y+25
        pivot_x=anchor_x+round(shoulder)
        d=ImageDraw.Draw(image)
        # Shoulder rolls inward during the cross-body strike, keeping the limb
        # attached to the original right shoulder with a short steel connector.
        d.polygon(((anchor_x+6,pivot_y-4),(pivot_x-5,pivot_y-4),
                   (pivot_x-6,pivot_y+5),(anchor_x+7,pivot_y+6)),fill="#4478b0",outline="#203955")
        d.polygon(((pivot_x-5,pivot_y-4),(pivot_x+6,pivot_y-4),
                   (pivot_x+9,pivot_y+4),(pivot_x,pivot_y+9),(pivot_x-6,pivot_y+4)),
                  fill="#4478b0",outline="#203955")
        return pivot_x,pivot_y

    def _punch_pose(self,image,cx,cy,angle):
        pivot_x,pivot_y=self._body_pose(image,cx,cy)
        _,sprite=self._arm_sprite(angle)
        image.alpha_composite(sprite,(pivot_x-64,pivot_y-64))
        return self._fist(pivot_x,pivot_y,angle,1)

    def _meteor_motion(self,seconds):
        phase,t=self.meteor_phase(seconds)
        e=_ease(t)
        # Angles continue upward from the right and sweep across to the left.
        # The slower lift and short strike give the punch its anticipation/snap.
        if phase=="charge": return 145*e,1+.25*e,3*e,-4*e,0
        if phase=="windup":
            # Reach the raised pose early, then hold and tremble under tension.
            raised=_ease(t/.18)
            tremble=4*math.sin(math.tau*5*t*1.12)*math.sin(math.pi*t)
            return 145+10*raised+tremble,1.25+.25*raised,3+2*raised,-4-2*raised,0
        if phase=="punch": return 155+110*e,1.5,5-17*e,-6+4*e,-14*e
        if phase=="impact":
            settle=_ease(max(0,(t-.25)/.75))  # Brief held impact before recoil.
            return 265+10*settle,1.5,-12-2*settle,-2+2*settle,-14
        if phase=="recover": return 275+85*e,1.5-.5*e,-14*(1-e),-2*math.sin(math.pi*t),-14*(1-e)
        return 0,1,0,0,0

    def _meteor_geometry(self,cx,cy,seconds):
        angle,reach,shift,lift,shoulder=self._meteor_motion(seconds)
        body_x,body_y=int(cx+shift),int(cy+lift)
        px=body_x-self.body.width//2+64+round(shoulder)
        py=body_y-self.body.height//2+25
        return body_x,body_y,px,py,angle,reach,self._fist(px,py,angle,reach)

    def _meteor_trail(self,image,cx,cy,seconds):
        phase=seconds%METEOR_DURATION
        strike_start,strike_end=METEOR_TIMING["punch"]
        if not strike_start<phase<strike_end+.14: return
        end=min(phase,strike_end)
        start=max(strike_start,end-.12)
        fade=max(0,(phase-strike_end)/.14)
        # One faint limb silhouette and one short arc keep the strike readable.
        points=[self._meteor_geometry(cx,cy,start+(end-start)*i/12)[-1] for i in range(13)]
        d=ImageDraw.Draw(image)
        if fade<.65:
            d.line(points,fill="#5599b8",width=6 if fade<.25 else 3)
            d.line(points,fill="#b8e7ee",width=3 if fade<.25 else 1)
            if fade<.25: d.line(points,fill="#f8ebad",width=1)
        for lag,coverage in ((.065,8),):
            stamp=end-lag
            if stamp<strike_start or fade>(coverage/16): continue
            _,_,px,py,angle,reach,_=self._meteor_geometry(cx,cy,stamp)
            key,sprite=self._arm_sprite(angle,reach)
            ghost_key=(*key,coverage)
            if ghost_key not in self.arm_ghosts:
                ghost=ImageOps.colorize(sprite.convert("L"),"#366985","#b8e7ee").convert("RGBA")
                ghost.putalpha(ImageChops.multiply(sprite.getchannel("A"),self.ghost_masks[coverage]))
                self.arm_ghosts[ghost_key]=ghost
            image.alpha_composite(self.arm_ghosts[ghost_key],(px-64,py-64))
        for i in range(3):
            stamp=start+(end-start)*i/3
            fx,fy=self._meteor_geometry(cx,cy,stamp)[-1]
            self._star(d,fx+(i%2)*5,fy-7-i%3*3+int(fade*10),2,"#e5c36b" if i%2 else "#8bd6e4")

    def _meteor(self,image,cx,cy,seconds):
        phase,t=self.meteor_phase(seconds)
        base_x,base_y=cx,cy
        cx,cy,pivot_x,pivot_y,angle,reach,(fist_x,fist_y)=self._meteor_geometry(cx,cy,seconds)
        self._body_pose(image,cx,cy,pivot_x-(cx-self.body.width//2+64))
        self._meteor_trail(image,base_x,base_y,seconds)
        _,sprite=self._arm_sprite(angle,reach)
        image.alpha_composite(sprite,(pivot_x-64,pivot_y-64))
        d=ImageDraw.Draw(image)
        if phase in ("charge","windup"):
            intensity=t if phase=="charge" else 1
            for i in range(5):
                # Rising sparks while the fist is still low; orbit only once
                # it has lifted clear of the ground and the menu's lower edge.
                theta=-math.pi+i*math.pi/4 if intensity<.4 else seconds*2+i*math.tau/5
                r=21-10*intensity
                self._star(d,fist_x+math.cos(theta)*r,fist_y+math.sin(theta)*r,2,"#f1d876")
            if intensity>.35:
                r=12+int(math.sin(seconds*12)*2)
                d.polygon(((fist_x-r,fist_y),(fist_x,fist_y-r),
                           (fist_x+r,fist_y),(fist_x,fist_y+r)),outline="#b8e7ee",width=1)
        if phase in ("punch","impact"):
            self._star(d,fist_x-3,fist_y-5,5,"#f6f2ca")
            self._star(d,fist_x+5,fist_y+2,3,"#8bd6e4")
        if phase=="impact":
            hit_x,hit_y=fist_x-12,fist_y-2
            r=6+int(t*14)
            if t<.55:
                self._star(d,hit_x,hit_y,17-int(t*15),"#e5c36b")
                self._star(d,hit_x,hit_y,10-int(t*8),"#fff5c9")
            for i in range(8):
                theta=i*math.tau/8
                px,py=hit_x+math.cos(theta)*r,hit_y+math.sin(theta)*r
                self._star(d,px,py,3 if t<.6 else 2,"#f6de8f" if i%2 else "#79bfdb")
        return cx,cy

    def _sign(self,image,cx,cy,seconds):
        lift=int(_ease(min(seconds/.45,1))*8)
        hand_x,hand_y=self._punch_pose(image,cx,cy,45)
        x,y=hand_x-24,hand_y-38-lift
        d=ImageDraw.Draw(image)
        d.rectangle((hand_x-1,y+22,hand_x+2,hand_y+3),fill="#795c45")
        d.rectangle((x,y,x+47,y+23),fill="#795c45",outline=INK,width=2)
        d.rectangle((x+3,y+3,x+44,y+20),fill="#f3dfaa")
        self.text(image,(x+6,y+5),"完成！",INK)
        for dx,dy in ((-11,-4),(54,5)):
            self._star(d,x+dx,y+dy+int(math.sin(seconds*5)*2),3,GOLD)

    def speech(self,image,cx,cy,state,seconds,demo=False):
        """A compact head-following bubble, not a fixed strip or name plate."""
        mark="?" if state=="asking" else "!" if state in ("failed","interrupted") else None
        phrases={
            "idle":("巡逻中～" if int(seconds/7)%2==0 else "休息一下",""),
            "working":("."*(int(seconds/.4)%6+1),""),
            "asking":("需要你决定","请查看 agent"),
            "completed":("任务完成啦！",""),
            "waiting":("稍等一会儿",""),
            "failed":("出错了！","请检查报错"),
            "interrupted":("任务中断","等待下一步"),
            "disconnected":("等你开工～",""),
            "unknown":("状态待确认",""),
        }
        first,second=phrases.get(state,phrases["unknown"])
        if demo: second=(second+" · " if second else "")+"演示"
        width=max(int(self.font.getlength(first)),int(self.font.getlength(second)))+20+(28 if mark else 0)
        if state=="working": width=max(width,56)  # Keep the dots' bubble still.
        height=50 if mark else 43 if second else 27
        left=max(4,min(image.width-width-4,int(cx-width/2)))
        top=max(3,int(cy-(83 if state=="working" else 65)-height/2))
        right,bottom=left+width,top+height
        tail_x=max(left+13,min(right-14,int(cx+7)))
        fill="#fff0b9" if state=="asking" else "#f1d5c5" if mark=="!" else "#e5eed7"
        d=ImageDraw.Draw(image)
        shape=((left+5,top),(right-5,top),(right-5,top+2),(right-2,top+2),
               (right-2,top+5),(right,top+5),(right,bottom-5),(right-2,bottom-5),
               (right-2,bottom-2),(right-5,bottom-2),(right-5,bottom),
               (tail_x+6,bottom),(tail_x,bottom+8),(tail_x-3,bottom),
               (left+5,bottom),(left+5,bottom-2),(left+2,bottom-2),
               (left+2,bottom-5),(left,bottom-5),(left,top+5),(left+2,top+5),
               (left+2,top+2),(left+5,top+2))
        d.polygon(shape,fill=fill,outline=INK,width=2)
        if mark:
            sprite=self.unown[mark]
            image.alpha_composite(sprite,(left+8,top+(height-sprite.height)//2+int(math.sin(seconds*3))))
            self.text(image,(left+35,top+10),first)
            self.text(image,(left+35,top+28),second,GREEN if mark=="?" else "#8e534a")
        else:
            self.text(image,(left+(width-int(self.font.getlength(first)))//2,top+7),first)
            if second: self.text(image,(left+(width-int(self.font.getlength(second)))//2,top+24),second,GREEN)

    def pack_scene(self,image,box,pack,state,seconds,transparent=False,bubble_state=None,demo=False):
        """Render a fixed pack canvas so changing frame bounds never cause jitter."""
        x,y,w,h=box
        if not transparent:
            d=ImageDraw.Draw(image)
            d.rectangle((x,y,x+w,y+h),fill="#d5e0c6")
            for gx in range(x+4,x+w,12):
                d.line((gx,y+h-10,gx+4,y+h-10),fill="#a5bbaa")
            self.text(image,(x+8,y+5),f"No.{pack.species_id:04d}","#739186")
        frames=pack.animations[state]
        durations=pack.durations[state]
        elapsed=int(seconds*1000)%sum(durations)
        index=0
        while index<len(durations)-1 and elapsed>=durations[index]:
            elapsed-=durations[index]
            index+=1
        frame=frames[index]
        available_h=h-(70 if transparent else 6)
        factor=min(1,(w-16)/frame.width,available_h/frame.height)
        if factor<1:
            frame=frame.resize((max(1,int(frame.width*factor)),max(1,int(frame.height*factor))),Image.Resampling.NEAREST)
        image.alpha_composite(frame,(x+(w-frame.width)//2,y+h-6-frame.height))
        if transparent:
            self.speech(image,x+w//2,y+h-76,bubble_state or state,seconds,demo)

    def scene(self,image,box,state,seconds,transparent=False,bubble_state=None,demo=False):
        x,y,w,h=box
        d=ImageDraw.Draw(image)
        if not transparent:
            d.rectangle((x,y,x+w,y+h),fill="#d5e0c6")
            for gx in range(x+4,x+w,12):
                d.line((gx,y+h-10,gx+4,y+h-10),fill="#a5bbaa")
            self.text(image,(x+8,y+5),"No.0376","#739186")
        cx,cy=x+w//2-12,y+h-(54 if transparent else 34 if state=="working" else 40)
        bob=int(math.sin(seconds*3)*2)
        if state=="working": bob=0
        if state=="idle": cx+=int(math.sin(seconds*.55)*12)
        if state=="asking": bob=int(math.sin(seconds*2))
        if state=="failed":
            cx-=int(abs(math.sin(min(seconds,.6)*7))*5)
            bob+=2
        if state=="interrupted": bob+=3
        ground=(25,30) if not transparent and state=="working" else (30,36)
        d.ellipse((cx-34,cy+ground[0],cx+34,cy+ground[1]),fill="#8fa99e")
        cy+=bob
        if state=="working": cx,cy=self._meteor(image,cx,cy,seconds)
        elif state=="completed": self._sign(image,cx,cy-int(abs(math.sin(seconds*3))*3),seconds)
        else:
            name="metagross-back" if state=="idle" and int(seconds/7)%3==1 else "metagross"
            frames=self.frames[name]
            frame=frames[int(seconds*(4 if state=="asking" else 8))%len(frames)]
            if state=="asking": frame=frame.rotate(3 if int(seconds/1.4)%2 else -3,Image.Resampling.NEAREST,expand=True)
            image.alpha_composite(frame,(int(cx-frame.width//2),int(cy-frame.height//2)))
            if state=="failed":
                for i in range(3): self._star(d,cx+43+i*7,cy+8+i*4+int((seconds*9)%7),2,"#b18e88")
        if transparent: self.speech(image,cx,cy,bubble_state or state,seconds,demo)


def panel_box(image,box,fill=PAPER,edge=INK):
    d=ImageDraw.Draw(image)
    x,y,w,h=box
    d.rectangle((x+2,y+2,x+w+2,y+h+2),fill=INK)
    d.rectangle((x,y,x+w,y+h),fill=fill,outline=edge,width=2)


def number(value):
    return f"{value/1_000_000:.2f}M" if value>=1_000_000 else (f"{value/1_000:.1f}K" if value>=10_000 else f"{value:,}")
