"""Automated UI journey, using fixtures only. This is not a human usability test."""
import json
import argparse
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--meteor-sweep",action="store_true")
    options=parser.parse_args()
    output=options.output or ROOT/".local"/("ui-journey-"+str(time.time_ns()))
    output.mkdir(parents=True)
    os.environ.update(POKETASK_DATA_DIR=str(output/"settings"),CODEX_HOME=str(output/"codex"),
                      CLAUDE_CONFIG_DIR=str(output/"claude"))
    from poketask.app import App
    from poketask.core import Session
    from poketask.visuals import STATES, METEOR_TIMING
    app=App(SimpleNamespace(demo=True,smoke_test=None))
    checks=[]
    try:
        app.stop.set()
        app.root.update()
        if options.meteor_sweep:
            original_clock=app.animation_time
            try:
                app.preview("working")
                timings=(("charge",.625),("windup",.35),("windup",.45),("punch",.25),
                         ("punch",.72),("impact",.125),("impact",.5),("recover",.5))
                for index,(phase,fraction) in enumerate(timings):
                    start,end=METEOR_TIMING[phase]
                    stamp=start+(end-start)*fraction
                    app.animation_time=lambda state,moment=stamp:moment
                    app.draw_panel()
                    app.draw_pet()
                    app.root.update_idletasks()
                    app.photo._PhotoImage__photo.write(str(output/(f"sweep-{index:02d}-panel.png")),format="png")
                    app.pet_photo._PhotoImage__photo.write(str(output/(f"sweep-{index:02d}-pet.png")),format="png")
                if sys.platform=="win32":
                    assert str(app.pet.attributes("-transparentcolor")).lower()=="#ff00ff"
                checks.append("meteor-sweep-checkpoint-render-and-native-color-key")
            finally:
                app.animation_time=original_clock
        for state in STATES:
            app.preview(state)
            app.draw_panel()
            app.draw_pet()
            app.photo._PhotoImage__photo.write(str(output/(state+".png")),format="png")
            app.pet_photo._PhotoImage__photo.write(str(output/(state+"-pet.png")),format="png")
        checks.append("all-nine-state-pages-and-bubbles-render")
        app.change_page("settings")
        app.draw_panel()
        before=app.sound
        app.focused=3
        app.focus_next(None)
        app.activate_focused(None)
        assert app.sound is not before
        saved=json.loads((output/"settings/settings.json").read_text(encoding="utf-8"))
        assert saved["sound"] is app.sound
        checks.append("keyboard-toggle-and-settings-persistence")
        app.sessions=[Session("alpha","codex","VS Code","Alpha",state="working",total_tokens=120),
                      Session("beta","claude","Desktop","Beta",state="failed",total_tokens=80)]
        app.change_page("agents")
        app.draw_panel()
        app.click(SimpleNamespace(x=100*app.scale,y=190*app.scale))
        assert app.current().id=="beta" and app.current().total_tokens==80 and not app.demo
        checks.append("agent-switch-clears-demo-and-isolates-usage")
        for page in ("status","usage","agents","settings","bridge","animations"):
            app.change_page(page)
            app.draw_panel()
            app.photo._PhotoImage__photo.write(str(output/(page+"-menu.png")),format="png")
        checks.append("all-six-views-render")
        app.hide_panel()
        assert not app.panel_visible
        app.show_panel()
        assert app.panel_visible
        checks.append("close-panel-and-reopen-pet")
        app.drag_start(SimpleNamespace(x_root=400,y_root=400))
        app.drag_move(SimpleNamespace(x_root=-10000,y_root=-10000))
        app.root.update_idletasks()
        app.drag_end(None)
        assert app.pet.winfo_x()>=0 and app.pet.winfo_y()>=0
        checks.append("pet-drag-clamped-to-screen")
        result={"passed":True,"checks":checks,"kind":"automated fixture UI journey; no human participant"}
        (output/"result.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        print(json.dumps({**result,"artifacts":str(output)},ensure_ascii=True))
    finally:
        app.quit()


if __name__=="__main__":
    main()
