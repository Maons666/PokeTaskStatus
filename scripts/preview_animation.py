"""Render the real pet animation for review, without launching desktop windows."""
from pathlib import Path
import argparse
import sys
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from poketask.visuals import PixelArt, PET_WIDTH, PET_HEIGHT, METEOR_TIMING, METEOR_DURATION


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/".local/adjustment-preview")
    output=parser.parse_args().output
    output.mkdir(parents=True,exist_ok=True)
    art=PixelArt()
    def render(state,seconds,background=(0,0,0,0)):
        canvas=Image.new("RGBA",(PET_WIDTH,PET_HEIGHT),background)
        art.scene(canvas,(0,0,PET_WIDTH,PET_HEIGHT),state,seconds,transparent=True)
        return canvas
    for state in ("idle","asking","failed","interrupted","completed"):
        render(state,.7).resize((PET_WIDTH*3,PET_HEIGHT*3),Image.Resampling.NEAREST).save(output/(state+".png"))
    sheet=Image.new("RGBA",(PET_WIDTH*3,PET_HEIGHT*2),"#d7e0db")
    labels=("01 抬起右拳","02 蓄力微晃","03 快速横扫","04 左侧命中","05 收势","06 待机")
    progress=(.625,.55,.72,.125,.5,.5)
    for index,(label,(start,end),fraction) in enumerate(zip(labels,METEOR_TIMING.values(),progress)):
        seconds=start+(end-start)*fraction
        frame=render("working",seconds,"#d7e0db")
        art.text(frame,(6,PET_HEIGHT-15),label)
        sheet.alpha_composite(frame,((index%3)*PET_WIDTH,(index//3)*PET_HEIGHT))
    sheet.resize((sheet.width*2,sheet.height*2),Image.Resampling.NEAREST).save(output/"meteor-storyboard.png")
    frames=[render("working",i/30,"#d7e0db").convert("RGB").resize((PET_WIDTH*3,PET_HEIGHT*3),Image.Resampling.NEAREST) for i in range(round(METEOR_DURATION*30))]
    durations=[30 if i%3<2 else 40 for i in range(len(frames))]
    frames[0].save(output/"meteor-mash.gif",save_all=True,append_images=frames[1:],duration=durations,loop=0,disposal=2)
    print(output)


if __name__=="__main__":
    main()
