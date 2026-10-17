"""Export the built-in choreography as a complete, declarative example pack."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import zipfile

from PIL import Image

from .visuals import PixelArt, METEOR_DURATION, PET_WIDTH, PET_HEIGHT, STATES


def _gif_frame(frame):
    # Reserve palette entry zero for transparency; keep opaque pixels opaque.
    palette_image=frame.convert("RGB").quantize(colors=255,dither=Image.Dither.NONE)
    result=Image.new("P",frame.size)
    palette=palette_image.getpalette()[:765]
    result.putpalette([0,0,0]+palette+[0]*(765-len(palette)))
    result.putdata([index+1 if alpha else 0 for index,alpha in
                    zip(palette_image.getdata(),frame.getchannel("A").getdata())])
    result.info["transparency"]=0
    return result


def export_builtin_pack(destination: Path) -> Path:
    """Write atomically; the caller owns any overwrite confirmation."""
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    art=PixelArt()
    # Bubbles and Unown symbols are shared application UI, not pack frames.
    art.speech=lambda *args,**kwargs:None
    manifest={"schema_version":1,"id":"metagross-example","name":"巨金怪",
        "species_id":376,"signature_move":"彗星拳","canvas":[192,112],
        "portrait":"portrait.png","animations":{state:state+".gif" for state in STATES},
        "sources":[
            "Pokémon Black/White official Metagross sprites, mirrored by PokeAPI/sprites: "
            "https://github.com/PokeAPI/sprites/tree/6e7928dcbd547d50b5c644fe76e093e8da98ac3f/sprites/pokemon/versions/generation-v/black-white",
            "PokeTask Status: original sprite rig, reconstructed hidden leg, Meteor Mash choreography and effects"
        ]}
    durations={"idle":(21,8),"working":(METEOR_DURATION,30),"asking":(2.8,12)}
    with tempfile.NamedTemporaryFile(prefix=".poketask-",suffix=".zip",dir=destination.parent,delete=False) as temp:
        temporary=Path(temp.name)
    try:
        with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("pet.json",json.dumps(manifest,ensure_ascii=False,indent=2).encode("utf-8"))
            portrait=BytesIO()
            art.frames["metagross"][0].save(portrait,format="PNG")
            archive.writestr("portrait.png",portrait.getvalue())
            for state in STATES:
                length,fps=durations.get(state,(2.5,12))
                count=round(length*fps)
                frames=[]
                times=[round(i*length/count*100)*10 for i in range(count+1)]
                for index in range(count):
                    frame=Image.new("RGBA",(PET_WIDTH,PET_HEIGHT))
                    art.scene(frame,(0,0,PET_WIDTH,PET_HEIGHT),state,index*length/count,transparent=True)
                    frames.append(_gif_frame(frame.crop((4,44,196,156))))
                output=BytesIO()
                frames[0].save(output,format="GIF",save_all=True,append_images=frames[1:],
                    duration=[b-a for a,b in zip(times,times[1:])],loop=0,
                    transparency=0,background=0,disposal=2,optimize=False)
                archive.writestr(state+".gif",output.getvalue())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
