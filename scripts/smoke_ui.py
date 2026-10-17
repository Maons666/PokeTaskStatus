"""Automated UI journey, using fixtures only. This is not a human usability test."""
import json
import argparse
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--meteor-sweep",action="store_true")
    parser.add_argument("--pet-pack",type=Path,help="Exercise import/select/render/persist with a complete ZIP pack")
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
        app.worker.join(timeout=3)
        app.root.update()
        assert app.root.overrideredirect()
        app.draw_panel()
        assert [box for box,_ in app.buttons[1:5]]==[(12,47,144,28),(164,47,144,28),(12,81,144,28),(164,81,144,28)]
        checks.append("frameless-panel-and-four-tabs-in-two-by-two-grid")
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
        app.focused=4
        app.focus_next(None)
        app.activate_focused(None)
        assert app.sound is not before
        saved=json.loads((output/"settings/settings.json").read_text(encoding="utf-8"))
        assert saved["sound"] is app.sound
        checks.append("keyboard-toggle-and-settings-persistence")
        while not app.queue.empty():
            app.queue.get_nowait()
        app.sessions=[Session("alpha","codex","VS Code","Alpha",state="working",total_tokens=120),
                      Session("beta","claude","Desktop","Beta",state="failed",total_tokens=80)]
        app.change_page("agents")
        app.draw_panel()
        app.click(SimpleNamespace(x=100*app.scale,y=190*app.scale))
        assert app.current().id=="beta" and app.current().total_tokens==80 and not app.demo
        checks.append("agent-switch-clears-demo-and-isolates-usage")
        for page in ("status","usage","agents","settings","bridge","animations","box"):
            app.change_page(page)
            app.draw_panel()
            app.photo._PhotoImage__photo.write(str(output/(page+"-menu.png")),format="png")
        checks.append("all-seven-views-render")
        if options.pet_pack:
            app.change_page("box")
            before_agent=(app.selected,app.current().total_tokens,app.sound,dict(app.gate.previous))
            with patch("poketask.app.filedialog.askopenfilename",return_value=str(options.pet_pack.resolve())):
                app.import_pet()
            deadline=time.monotonic()+30
            while app.pack_busy and time.monotonic()<deadline:
                app.root.update()
                time.sleep(.02)
            assert not app.pack_busy and app.preview_pack and not app.box_error
            app.use_pet()
            imported_id=app.selected_pet
            assert imported_id!="builtin-metagross"
            assert (app.selected,app.current().total_tokens,app.sound,app.gate.previous)==before_agent
            saved=json.loads((output/"settings/settings.json").read_text(encoding="utf-8"))
            assert saved["selected_pet"]==imported_id
            app.draw_panel()
            app.photo._PhotoImage__photo.write(str(output/"imported-box.png"),format="png")
            for state in STATES:
                app.preview(state)
                app.draw_panel()
                app.draw_pet()
                app.pet_photo._PhotoImage__photo.write(str(output/("imported-"+state+".png")),format="png")
            app.choose_pet(app.catalog[0])
            app.use_pet()
            assert app.active_pack is None and app.selected_pet=="builtin-metagross"
            checks.append("complete-pack-import-select-nine-animations-persist-and-restore-built-in")
        app.hide_panel()
        assert not app.panel_visible
        app.show_panel()
        assert app.panel_visible
        checks.append("close-panel-and-reopen-pet")
        app.panel_press(SimpleNamespace(x=80*app.scale,y=20*app.scale,x_root=400,y_root=400))
        app.panel_move(SimpleNamespace(x_root=-10000,y_root=-10000))
        app.root.update_idletasks()
        app.panel_release(None)
        assert app.root.winfo_x()>=0 and app.root.winfo_y()>=0
        checks.append("frameless-menu-drag-and-save-position")
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
