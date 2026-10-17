"""Windows desktop pet and Pokédex-style control panel. No network at runtime."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
from PIL import Image, ImageDraw, ImageTk
from .core import Session, QuotaWindow
from .monitor import Monitor
from .paths import assets_dir, data_dir
from .petpacks import discover_pack_summaries, import_pack, load_pack
from .visuals import (PixelArt, STATES, INK, PAPER, MINT, GREEN, RED, GOLD,
                      PET_WIDTH, PET_HEIGHT, TRANSPARENT_KEY, panel_box, number)


class CompletionGate:
    """Never sound for startup history, switching, stale events or duplicate polls."""
    def __init__(self):
        self.started = time.time()
        self.previous = {}

    def update(self, sessions, selected):
        notify = False
        for session in sessions:
            key = session.provider + ":" + session.id
            before = self.previous.get(key)
            if session.completion_id:
                fingerprint = ("event", session.completion_id)
                completed = session.completed_at
                changed = before is not None and before != fingerprint
            else:
                # Compatibility for demo/manual snapshots without event metadata.
                fingerprint = (session.state, session.updated_at)
                completed = session.updated_at if session.state == "completed" else 0
                changed = before is not None and (before[0] != "completed" or completed > before[1])
            if (key == selected and changed and completed >= self.started and
                    not session.usage_partial):
                notify = True
            self.previous[key] = fingerprint
        return notify


class App:
    W, H = 320, 402

    def __init__(self, args):
        self.args = args
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("PokeTask · 图鉴")
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.configure(bg=INK)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_panel)
        self.root.bind("<Escape>", lambda e: self.hide_panel())
        self.store = data_dir()
        self.config = self.load_config()
        self.scale = self.config_int("scale", 2, 1, 3)
        if self.root.winfo_screenheight() < self.H*self.scale+80:
            self.scale = 1
        self.art = PixelArt()
        self.canvas = tk.Canvas(self.root, width=self.W*self.scale, height=self.H*self.scale,
                                highlightthickness=0, bg=INK)
        self.canvas.pack()
        self.surface = self.canvas.create_image(0,0,anchor="nw")
        self.canvas.bind("<Button-1>", self.panel_press)
        self.canvas.bind("<B1-Motion>", self.panel_move)
        self.canvas.bind("<ButtonRelease-1>", self.panel_release)
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.root.bind("<Tab>", self.focus_next)
        self.root.bind("<Return>", self.activate_focused)
        self.root.bind("<F1>", lambda e: self.open_help())
        self.buttons = []
        self.focused = -1
        self.page = "status"
        self.panel_drag = None
        self.offset = 0
        self.sessions = []
        self.selected = str(self.config.get("selected", ""))
        self.errors = []
        self.demo = bool(args.demo)
        self.preview_state = "working"
        self.queue = queue.Queue(maxsize=2)
        self.stop = threading.Event()
        self.gate = CompletionGate()
        self.started = time.monotonic()
        self.visual_key = None
        self.visual_started = self.started
        self.notice = ""
        self.notice_until = 0
        self.pack_store=self.store/"pets"
        self.pack_results=queue.Queue()
        self.pack_busy=False
        self.box_error=""
        self.box_offset=0
        self.active_pack=None
        self.preview_pack=None
        self.selected_pet=str(self.config.get("selected_pet","builtin-metagross"))
        self.box_candidate=self.selected_pet
        self.refresh_pets()
        if self.selected_pet!="builtin-metagross":
            try:
                entry=next(p for p in self.catalog if p["id"]==self.selected_pet)
                self.active_pack=load_pack(entry["directory"])
                self.preview_pack=self.active_pack
            except (StopIteration,OSError,ValueError) as exc:
                self.selected_pet=self.box_candidate="builtin-metagross"
                self.box_error="形象包不可用，已恢复巨金怪"
        self.bridge_health=(False,False)
        self.bridge_health_at=0
        self.panel_visible = False
        self.sound = self.config.get("sound", True) is not False
        self.topmost = self.config.get("topmost", True) is not False
        self.pet = tk.Toplevel(self.root)
        self.pet.title("PokeTask · 桌宠")
        self.pet.overrideredirect(True)
        self.pet.configure(bg=TRANSPARENT_KEY)
        self.pet.attributes("-topmost", self.topmost)
        if sys.platform == "win32":
            self.pet.attributes("-transparentcolor", TRANSPARENT_KEY)
        self.pet_canvas = tk.Canvas(self.pet,width=PET_WIDTH*self.scale,height=PET_HEIGHT*self.scale,
                                    bg=TRANSPARENT_KEY,highlightthickness=0)
        self.pet_canvas.pack()
        self.pet_surface = self.pet_canvas.create_image(0,0,anchor="nw")
        self.pet_canvas.bind("<ButtonPress-1>",self.drag_start)
        self.pet_canvas.bind("<B1-Motion>",self.drag_move)
        self.pet_canvas.bind("<ButtonRelease-1>",self.drag_end)
        self.pet_canvas.bind("<Double-Button-1>",lambda e: self.show_panel())
        self.pet_canvas.bind("<Button-3>",self.menu)
        self.drag = None
        self.moved = False
        sw,sh = self.root.winfo_screenwidth(),self.root.winfo_screenheight()
        x = self.config_int("pet_x",sw-(PET_WIDTH+20)*self.scale,0,sw)
        y = self.config_int("pet_y",sh-(PET_HEIGHT+18)*self.scale-48,0,sh)
        self.pet.geometry(f"+{max(0,min(sw-PET_WIDTH*self.scale,x))}+{max(0,min(sh-PET_HEIGHT*self.scale-40,y))}")
        px=self.config_int("panel_x",max(0,(sw-self.W*self.scale)//2),0,sw)
        py=self.config_int("panel_y",max(0,(sh-self.H*self.scale)//2-30),0,sh)
        self.root.geometry(f"+{min(px,max(0,sw-self.W*self.scale))}+{min(py,max(0,sh-self.H*self.scale))}")
        self.worker = threading.Thread(target=self.watch,daemon=True,name="local-log-observer")
        self.worker.start()
        if args.demo or args.smoke_test or not self.config.get("has_started"):
            self.show_panel()
        self.root.after(30,self.tick)
        if args.smoke_test:
            self.root.after(2400,self.smoke_snapshot)

    def load_config(self):
        try:
            value=json.loads((self.store/"settings.json").read_text(encoding="utf-8"))
            return value if isinstance(value,dict) else {}
        except (OSError,ValueError):
            return {}

    def config_int(self,key,default,low,high):
        try:
            return max(low,min(high,int(self.config.get(key,default))))
        except (ValueError,TypeError,OverflowError):
            return default

    def save(self):
        self.config.update(sound=self.sound,topmost=self.topmost,selected=self.selected,
                           selected_pet=self.selected_pet,panel_x=self.root.winfo_x(),panel_y=self.root.winfo_y(),
                           has_started=True,pet_x=self.pet.winfo_x(),pet_y=self.pet.winfo_y())
        try:
            self.store.mkdir(parents=True,exist_ok=True)
            temp=self.store/"settings.tmp"
            temp.write_text(json.dumps(self.config,ensure_ascii=False,indent=2),encoding="utf-8")
            temp.replace(self.store/"settings.json")
        except OSError:
            self.toast("无法保存设置，请检查目录权限")

    def watch(self):
        monitor=Monitor(Path(self.config["codex_root"]) if isinstance(self.config.get("codex_root"),str) and self.config["codex_root"] else None,
                        Path(self.config["claude_root"]) if isinstance(self.config.get("claude_root"),str) and self.config["claude_root"] else None,
                        bridge_root=self.store/"bridge")
        while not self.stop.is_set():
            try:
                sessions=monitor.poll()
                result=(sessions,list(monitor.errors))
            except Exception as exc:
                result=([],["读取失败："+type(exc).__name__])
            try:
                self.queue.put_nowait(result)
            except queue.Full:
                pass
            self.stop.wait(1)

    @staticmethod
    def key(session):
        return session.provider+":"+session.id

    def current(self):
        if self.demo:
            return Session("demo", "codex", "VS Code", "演示会话",state=self.preview_state,
                updated_at=time.time(),input_tokens=18920,output_tokens=2840,cached_tokens=12200,
                total_tokens=21760,context_percent=23.0,
                quota=[QuotaWindow("5 小时",32,time.time()+7200),QuotaWindow("每周",18,time.time()+86400)],
                detail="")
        return next((s for s in self.sessions if self.key(s)==self.selected),None)

    def tick(self):
        try:
            while True:
                callback,value,error=self.pack_results.get_nowait()
                self.pack_busy=False
                if error:
                    self.box_error=error
                    self.toast(error)
                    self.show_panel()
                    messagebox.showerror("形象包未完成",error,parent=self.root)
                else:
                    self.box_error=""
                    callback(value)
        except queue.Empty:
            pass
        signal=self.store/"show-panel.signal"
        if signal.exists():
            try:
                signal.unlink()
            except OSError:
                pass
            self.show_panel()
        try:
            while True:
                sessions,self.errors=self.queue.get_nowait()
                if not self.selected and sessions:
                    self.selected=self.key(sessions[0])
                if self.gate.update(sessions,self.selected) and self.sound and not self.demo:
                    self.play_sound()
                    self.toast("本轮完成")
                self.sessions=sessions
        except queue.Empty:
            pass
        try:
            self.draw_pet()
            if self.panel_visible:
                self.draw_panel()
        finally:
            session=self.current()
            self.root.after(33 if session and session.state=="working" else 66,self.tick)

    def play_sound(self):
        def play():
            try:
                import winsound
                # Original short notification, not extracted game audio.
                for frequency,length in ((784,100),(988,100),(1175,180)):
                    winsound.Beep(frequency,length)
            except (ImportError,RuntimeError):
                pass
        threading.Thread(target=play,daemon=True).start()

    def button(self,image,box,label,action,active=False,icon=None):
        x,y,w,h=box
        fill=MINT if active else PAPER
        panel_box(image,box,fill)
        if icon:
            sprite=self.art.items[icon]
            sprite=sprite.crop(sprite.getbbox())
            if sprite.height>h-4:
                factor=(h-4)/sprite.height
                sprite=sprite.resize((max(1,int(sprite.width*factor)),h-4),Image.Resampling.NEAREST)
            image.alpha_composite(sprite,(x+5,y+(h-sprite.height)//2))
        self.art.text(image,(x+(31 if icon else 5),y+(h-12)//2),label)
        self.buttons.append((box,action))
        if self.focused==len(self.buttons)-1:
            ImageDraw.Draw(image).rectangle((x+3,y+3,x+w-3,y+h-3),outline=RED)

    def item_tile(self,image,box,title,subtitle,icon,action):
        x,y,w,h=box
        panel_box(image,box,"#e8dec2")
        d=ImageDraw.Draw(image)
        d.rectangle((x+3,y+3,x+42,y+h-3),fill="#c7b68c")
        sprite=self.art.items[icon]
        sprite=sprite.crop(sprite.getbbox())
        factor=2 if sprite.height*2<=h-7 and sprite.width*2<=36 else 1
        sprite=sprite.resize((sprite.width*factor,sprite.height*factor),Image.Resampling.NEAREST)
        image.alpha_composite(sprite,(x+23-sprite.width//2,y+h//2-sprite.height//2))
        self.art.text(image,(x+48,y+7),self.art.fit(title,w-53))
        self.art.text(image,(x+48,y+25),self.art.fit(subtitle,w-53),GREEN)
        self.buttons.append((box,action))
        if self.focused==len(self.buttons)-1:
            d.rectangle((x+3,y+3,x+w-3,y+h-3),outline=RED)

    def animation_time(self,state):
        key=(self.selected_pet,"demo" if self.demo else self.selected,state)
        if key!=self.visual_key:
            self.visual_key=key
            self.visual_started=time.monotonic()
        return time.monotonic()-self.visual_started

    @property
    def move_name(self):
        return self.active_pack.signature_move if self.active_pack else "彗星拳"

    def refresh_pets(self):
        packs,errors=discover_pack_summaries(self.pack_store)
        self.catalog=[{"id":"builtin-metagross","name":"巨金怪","species_id":376,
                       "signature_move":"彗星拳","directory":None}]+packs
        if errors:
            self.box_error=f"{len(errors)} 个形象包不可用"

    def draw_creature(self,image,box,state,seconds,**options):
        if self.active_pack:
            self.art.pack_scene(image,box,self.active_pack,state,seconds,**options)
        else:
            self.art.scene(image,box,state,seconds,**options)

    def pack_task(self,work,done):
        if self.pack_busy:
            return
        self.pack_busy=True
        def run():
            try:
                result=work()
            except Exception as exc:
                self.pack_results.put((done,None,str(exc) or "形象包处理失败"))
            else:
                self.pack_results.put((done,result,None))
        # Let atomic import/export cleanup finish even when the UI is closed.
        threading.Thread(target=run,daemon=False,name="pet-pack-worker").start()

    def choose_pet(self,entry):
        if self.pack_busy:
            return
        def chosen(pack):
            self.box_candidate=entry["id"]
            self.preview_pack=pack
            self.box_error=""
        if entry["id"]=="builtin-metagross":
            chosen(None)
        elif entry["id"]==self.selected_pet:
            chosen(self.active_pack)
        else:
            self.pack_task(lambda:load_pack(entry["directory"]),chosen)

    def use_pet(self):
        if self.pack_busy or self.box_candidate==self.selected_pet:
            return
        self.selected_pet=self.box_candidate
        self.active_pack=self.preview_pack
        self.visual_key=None
        self.save()
        self.toast("已使用 "+(self.active_pack.name if self.active_pack else "巨金怪"))

    def import_pet(self):
        if self.pack_busy:
            return
        path=filedialog.askopenfilename(title="导入宝可梦形象包",parent=self.root,
                                       filetypes=[("宝可梦形象包","*.zip")])
        if not path:
            return
        def imported(pack):
            self.refresh_pets()
            self.box_candidate=pack.id
            self.preview_pack=pack
            index=next(i for i,p in enumerate(self.catalog) if p["id"]==pack.id)
            self.box_offset=index//4*4
            self.toast("已导入 "+pack.name)
        self.pack_task(lambda:import_pack(Path(path),self.pack_store),imported)

    def export_pet(self):
        if self.pack_busy:
            return
        path=filedialog.asksaveasfilename(title="导出巨金怪示例包",parent=self.root,
            initialfile="metagross.pokepet.zip",defaultextension=".zip",
            filetypes=[("宝可梦形象包","*.zip")])
        if path:
            from .packexport import export_builtin_pack
            self.pack_task(lambda:export_builtin_pack(Path(path)),lambda _:self.toast("示例包已导出"))

    def shift_box(self,delta):
        last=max(0,(len(self.catalog)-1)//4*4)
        self.box_offset=max(0,min(last,self.box_offset+delta))

    def draw_box(self,im):
        for i,entry in enumerate(self.catalog[self.box_offset:self.box_offset+4]):
            x,y=12+i%2*152,124+i//2*56
            box=(x,y,144,48)
            panel_box(im,box,MINT if entry["id"]==self.selected_pet else PAPER,
                      GOLD if entry["id"]==self.box_candidate else INK)
            self.art.item(im,"poke-ball",x+3,y+6)
            self.art.text(im,(x+32,y+7),self.art.fit(entry["name"],105))
            caption="使用中" if entry["id"]==self.selected_pet else entry["signature_move"]
            self.art.text(im,(x+32,y+27),self.art.fit(caption,105),GREEN)
            self.buttons.append((box,lambda p=entry:self.choose_pet(p)))
            if self.focused==len(self.buttons)-1:
                ImageDraw.Draw(im).rectangle((x+3,y+3,x+141,y+45),outline=RED)
        self.button(im,(12,236,62,18),"上一页",lambda:self.shift_box(-4))
        self.art.text(im,(132,238),f"{self.box_offset//4+1} / {(len(self.catalog)+3)//4}")
        self.button(im,(246,236,62,18),"下一页",lambda:self.shift_box(4))
        entry=next((p for p in self.catalog if p["id"]==self.box_candidate),self.catalog[0])
        panel_box(im,(12,264,296,68))
        portrait=self.preview_pack.portrait if self.preview_pack else self.art.frames["metagross"][0]
        portrait=portrait.crop(portrait.getbbox())
        factor=min(1,88/portrait.width,56/portrait.height)
        portrait=portrait.resize((max(1,int(portrait.width*factor)),max(1,int(portrait.height*factor))),Image.Resampling.NEAREST)
        im.alpha_composite(portrait,(60-portrait.width//2,299-portrait.height//2))
        self.art.text(im,(112,272),self.art.fit(entry["name"],184))
        self.art.text(im,(112,292),self.art.fit("招式 · "+entry["signature_move"],184),GREEN)
        self.art.text(im,(112,312),f"No.{entry['species_id']:04d} · 9 种状态",GREEN)
        self.button(im,(12,344,94,26),"导入",self.import_pet)
        self.button(im,(113,344,94,26),"已使用" if self.box_candidate==self.selected_pet else "使用",self.use_pet,
                    self.box_candidate==self.selected_pet)
        self.button(im,(214,344,94,26),"导出示例",self.export_pet)

    def draw_panel(self):
        im=Image.new("RGBA",(self.W,self.H),RED)
        d=ImageDraw.Draw(im)
        d.rectangle((3,3,316,398),outline=INK,width=2)
        d.rectangle((8,8,311,39),fill=INK)
        self.art.item(im,"poke-ball",9,8)
        self.art.text(im,(42,13),"POKETASK",PAPER)
        self.buttons=[]
        self.button(im,(265,12,36,19),"收起",self.hide_panel)
        for i,(label,page,icon) in enumerate((("图鉴","status","poke-radar"),("队伍","agents","poke-ball"),("背包","settings","silph-scope"),("盒子","box","vs-recorder"))):
            active=self.page==page or (page=="settings" and self.page in ("bridge","usage","animations"))
            self.button(im,(12+i%2*152,47+i//2*34,144,28),label,lambda p=page:self.change_page(p),active,icon=icon)
        if self.page=="status":
            self.draw_status(im)
        elif self.page=="agents":
            self.draw_agents(im)
        elif self.page=="box":
            self.draw_box(im)
        else:
            self.draw_settings(im)
        text="处理中…" if self.pack_busy else self.notice if time.monotonic()<self.notice_until else self.box_error if self.page=="box" and self.box_error else (self.errors[0] if self.errors else "演示" if self.demo else "")
        if text:
            self.art.text(im,(12,381),self.art.fit(text,296),INK)
        self.photo=ImageTk.PhotoImage(im.convert("RGB").resize((self.W*self.scale,self.H*self.scale),Image.Resampling.NEAREST))
        self.canvas.itemconfigure(self.surface,image=self.photo)

    def draw_status(self,im):
        s=self.current()
        state=s.state if s else "disconnected"
        name,color=STATES.get(state,STATES["unknown"])
        animation="idle" if s and state=="completed" and not self.demo and time.time()-s.updated_at>4 else state
        self.draw_creature(im,(14,120,292,110),animation,self.animation_time(state))
        panel_box(im,(12,233,296,33))
        detail=s.detail if s and not self.demo and state in ("failed","interrupted","unknown") else ""
        if not detail and s:
            detail=f"{s.provider.upper()} / {s.source} · {s.name}"
        status_y=237 if detail else 244
        ImageDraw.Draw(im).rectangle((20,status_y+5,25,status_y+10),fill=color)
        self.art.text(im,(31,status_y),name+(" · 演示" if self.demo else ""))
        age=max(0,int(time.time()-s.updated_at)) if s else None
        self.art.text(im,(190,status_y),f"{age//60}分钟前" if age is not None and age>=60 else ("刚刚更新" if s else "等待连接"),GREEN)
        if detail:
            self.art.text(im,(20,251),self.art.fit(detail,278),"#657b77")
        panel_box(im,(12,275,296,61))
        observed=max((q.observed_at for q in s.quota),default=0) if s else 0
        try:
            quota_title="额度快照 · "+time.strftime("%m-%d %H:%M",time.localtime(observed)) if observed else "账户额度 · 等待上报"
        except (ValueError,OverflowError,OSError):
            quota_title="账户额度 · 时间未知"
        self.art.text(im,(20,280),quota_title,GREEN)
        if s and s.quota:
            for i,q in enumerate(s.quota[:2]):
                y=297+i*17
                stale=(time.time()-q.observed_at>900 or (q.resets_at is not None and time.time()>=q.resets_at))
                remaining=max(0,100-q.used_percent)
                label=f"{q.label} 剩余 {remaining:g}%"+(" [旧]" if stale else "")
                self.art.text(im,(20,y),self.art.fit(label,176),GOLD if stale else INK)
                d=ImageDraw.Draw(im)
                d.rectangle((202,y+3,297,y+10),fill=INK)
                d.rectangle((204,y+5,204+int(91*remaining/100),y+8),fill=GOLD if stale else GREEN)
        else:
            self.art.text(im,(20,303),"未知 · 来源未提供额度")
        self.button(im,(12,345,145,25),"代币盒 / 用量",lambda:self.change_page("usage"),icon="coin-case")
        self.button(im,(164,345,144,25),"声音："+("开" if self.sound else "关"),self.toggle_sound,icon="poke-flute")

    def draw_agents(self,im):
        if not self.sessions:
            self.art.text(im,(20,151),"暂无会话")
        self.offset=max(0,min(self.offset,max(0,len(self.sessions)-4)))
        for i,s in enumerate(self.sessions[self.offset:self.offset+4]):
            y=124+i*49
            active=self.key(s)==self.selected
            box=(12,y,296,42)
            panel_box(im,box,MINT if active else PAPER)
            self.art.item(im,"poke-ball",16,y+6)
            self.art.text(im,(49,y+5),self.art.fit(f"{s.provider.upper()} / {s.source} · {STATES[s.state][0]}",251))
            self.art.text(im,(49,y+23),self.art.fit(f"{s.name} · {s.id[:12]}",251),GREEN)
            self.buttons.append((box,lambda session=s:self.select(session)))
            if self.focused==len(self.buttons)-1:
                ImageDraw.Draw(im).rectangle((15,y+3,305,y+39),outline=RED)
        self.button(im,(12,344,94,26),"上一页",lambda:self.shift(-4))
        self.button(im,(113,344,94,26),"下一页",lambda:self.shift(4))
        self.button(im,(214,344,94,26),"当前图鉴",lambda:self.change_page("status"))

    def draw_settings(self,im):
        if self.page=="usage":
            return self.draw_usage(im)
        if self.page=="bridge":
            return self.draw_bridge(im)
        if self.page=="animations":
            return self.draw_animations(im)
        tiles=[("宝可梦之笛","声音："+("开" if self.sound else "关"),"poke-flute",self.toggle_sound),
               ("气球","置顶："+("开" if self.topmost else "关"),"air-balloon",self.toggle_top),
               ("城镇地图","Codex 目录","town-map",lambda:self.choose_root("codex")),
               ("城镇地图","Claude 目录","town-map",lambda:self.choose_root("claude")),
               ("西尔佛检视镜","Claude 接入","silph-scope",lambda:self.change_page("bridge")),
               ("宝可追踪器","接入与说明","poke-radar",self.open_help),
               ("对战记录器","动画预览","vs-recorder",lambda:self.change_page("animations")),
               ("离洞绳","退出程序","escape-rope",self.quit)]
        for i,(title,subtitle,icon,action) in enumerate(tiles):
            self.item_tile(im,(12+(i%2)*152,128+(i//2)*51,144,44),title,subtitle,icon,action)
        self.button(im,(12,342,145,28),"试听完成音",self.play_sound,icon="poke-flute")
        self.button(im,(164,342,144,28),"只显示桌宠",self.hide_panel,icon="poke-ball")

    def draw_animations(self,im):
        states=(("空闲","idle"),(self.move_name,"working"),("询问 ?","asking"),
                ("本轮完成","completed"),("普通等待","waiting"),("中断 !","interrupted"),
                ("失败 !","failed"),("状态待确认","unknown"),("等待连接","disconnected"))
        for i,(label,state) in enumerate(states):
            x,y=12+(i%3)*101,132+(i//3)*64
            panel_box(im,(x,y,94,54),MINT if self.demo and self.preview_state==state else PAPER)
            if state in ("asking","failed","interrupted"):
                sprite=self.art.unown["?" if state=="asking" else "!"]
                # Original Unown fits the preview slot without replacing it with text.
                im.alpha_composite(sprite,(x+7,y+8))
                self.art.text(im,(x+29,y+20),label)
            else:
                self.art.item(im,"vs-recorder",x+2,y+10)
                self.art.text(im,(x+30,y+20),self.art.fit(label,61))
            self.buttons.append(((x,y,94,54),lambda st=state:self.preview(st)))
            if self.focused==len(self.buttons)-1:
                ImageDraw.Draw(im).rectangle((x+3,y+3,x+91,y+51),outline=RED)
        self.button(im,(12,342,145,28),"返回真实会话",self.leave_demo,icon="poke-ball")
        self.button(im,(164,342,144,28),"返回背包",lambda:self.change_page("settings"),icon="silph-scope")

    def leave_demo(self):
        self.demo=False
        self.change_page("status")

    def draw_bridge(self,im):
        panel_box(im,(12,120,296,209))
        lines=["可增加询问授权、失败与结束事件。",
               "状态栏有提供额度时才显示剩余值。",
               "点击安装会合并 Claude settings.json，",
               "自动备份，并保留现有 hooks。",
               "已有自定义状态栏时不会覆盖。",
               "Desktop / IDE 未触发状态栏时，",
               "额度仍显示未知。安装不等于连通。",
               "安装后重开 Claude 会话。",
               "移除只撤销本应用加入的配置。"]
        for i,line in enumerate(lines):
            self.art.text(im,(22,128+i*18),line)
        if time.monotonic()-self.bridge_health_at>3:
            from .bridge import apply_bridge
            observed=apply_bridge([],self.store/"bridge")
            self.bridge_health=(any(bool(s.detail) for s in observed),any(bool(s.quota) for s in observed))
            self.bridge_health_at=time.monotonic()
        events,quota=self.bridge_health
        self.art.text(im,(22,300),"事件："+("已收到" if events else "未收到")+" / 额度："+("已收到" if quota else "未收到"),GREEN)
        self.button(im,(12,344,94,26),"安装接入",lambda:self.change_bridge(True))
        self.button(im,(113,344,94,26),"移除接入",lambda:self.change_bridge(False))
        self.button(im,(214,344,94,26),"返回背包",lambda:self.change_page("settings"))

    def change_bridge(self,install):
        from .bridge import install_bridge, uninstall_bridge
        configured=self.config.get("claude_root")
        root=Path(configured) if isinstance(configured,str) and configured else Path(os.environ.get("CLAUDE_CONFIG_DIR",str(Path.home()/".claude")))
        if root.name=="projects":
            root=root.parent
        try:
            result=(install_bridge if install else uninstall_bridge)(root/"settings.json",self.store)
            messagebox.showinfo("Claude 增强接入",result,parent=self.root)
        except (OSError,ValueError,RuntimeError) as exc:
            messagebox.showerror("无法更新接入",str(exc),parent=self.root)

    def draw_usage(self,im):
        s=self.current()
        panel_box(im,(12,120,296,209))
        if s:
            rows=[("累计输入",number(s.input_tokens)),("其中缓存读取",number(s.cached_tokens)),
                  ("累计输出",number(s.output_tokens)),("累计总量",number(s.total_tokens)),
                  ("最近上下文占用",f"{s.context_percent:.1f}%" if s.context_percent is not None else "未知")]
            for i,(label,value) in enumerate(rows):
                self.art.text(im,(22,135+i*23),label,GREEN)
                self.art.text(im,(207,135+i*23),value)
            if s.usage_partial:
                self.art.text(im,(22,244),"历史载入中",GREEN)
            for i,q in enumerate(s.quota[:2]):
                try:
                    reset=time.strftime("%m-%d %H:%M",time.localtime(q.resets_at)) if q.resets_at else "未知"
                except (ValueError,OverflowError,OSError):
                    reset="未知"
                self.art.text(im,(22,285+i*17),self.art.fit(q.label+" 重置："+reset,274))
        else:
            self.art.text(im,(22,133),"未选择会话")
        self.button(im,(12,344,145,26),"官方用量页面",self.open_usage)
        self.button(im,(164,344,144,26),"返回图鉴",lambda:self.change_page("status"))

    def draw_pet(self):
        im=Image.new("RGBA",(PET_WIDTH,PET_HEIGHT),TRANSPARENT_KEY)
        s=self.current()
        state=s.state if s else "disconnected"
        animation="idle" if s and state=="completed" and not self.demo and time.time()-s.updated_at>4 else state
        self.draw_creature(im,(0,0,PET_WIDTH,PET_HEIGHT),animation,self.animation_time(state),
                       transparent=True,bubble_state=state,demo=self.demo)
        self.pet_photo=ImageTk.PhotoImage(im.convert("RGB").resize((PET_WIDTH*self.scale,PET_HEIGHT*self.scale),Image.Resampling.NEAREST))
        self.pet_canvas.itemconfigure(self.pet_surface,image=self.pet_photo)

    def change_page(self,page):
        self.page=page
        self.focused=-1

    def select(self,s):
        self.selected=self.key(s)
        self.demo=False
        self.save()
        self.change_page("status")

    def scroll(self,event):
        if self.page=="agents":
            self.shift(-1 if event.delta>0 else 1)
        elif self.page=="box":
            self.shift_box(-4 if event.delta>0 else 4)

    def shift(self,delta):
        self.offset=max(0,min(max(0,len(self.sessions)-4),self.offset+delta))

    def click(self,event):
        x,y=event.x/self.scale,event.y/self.scale
        for (bx,by,w,h),action in self.buttons:
            if bx<=x<=bx+w and by<=y<=by+h:
                action()
                return

    def panel_press(self,event):
        if 8<=event.x/self.scale<257 and 8<=event.y/self.scale<=39:
            self.panel_drag=(event.x_root,event.y_root,self.root.winfo_x(),self.root.winfo_y())
        else:
            self.click(event)

    def panel_move(self,event):
        if self.panel_drag:
            x,y,px,py=self.panel_drag
            nx=max(0,min(self.root.winfo_screenwidth()-self.W*self.scale,px+event.x_root-x))
            ny=max(0,min(self.root.winfo_screenheight()-self.H*self.scale,py+event.y_root-y))
            self.root.geometry(f"+{nx}+{ny}")

    def panel_release(self,event):
        if self.panel_drag:
            self.panel_drag=None
            self.save()

    def focus_next(self,event):
        self.focused=(self.focused+1)%max(1,len(self.buttons))
        return "break"

    def activate_focused(self,event):
        if 0<=self.focused<len(self.buttons):
            self.buttons[self.focused][1]()

    def drag_start(self,event):
        self.drag=(event.x_root,event.y_root,self.pet.winfo_x(),self.pet.winfo_y())
        self.moved=False

    def drag_move(self,event):
        if self.drag:
            x,y,px,py=self.drag
            dx,dy=event.x_root-x,event.y_root-y
            self.moved=self.moved or abs(dx)+abs(dy)>4
            self.pet.geometry(f"+{max(0,min(self.root.winfo_screenwidth()-PET_WIDTH*self.scale,px+dx))}+{max(0,min(self.root.winfo_screenheight()-PET_HEIGHT*self.scale-40,py+dy))}")

    def drag_end(self,event):
        self.drag=None
        if self.moved:
            self.save()
        else:
            self.show_panel()

    def menu(self,event):
        menu=tk.Menu(self.pet,tearoff=False)
        menu.add_command(label="打开图鉴",command=self.show_panel)
        menu.add_command(label="声音："+("开" if self.sound else "关"),command=self.toggle_sound)
        menu.add_command(label="退出 PokeTask",command=self.quit)
        menu.tk_popup(event.x_root,event.y_root)

    def show_panel(self):
        self.panel_visible=True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_panel(self):
        self.panel_visible=False
        self.root.withdraw()
        self.save()

    def toggle_sound(self):
        self.sound=not self.sound
        self.save()

    def toggle_top(self):
        self.topmost=not self.topmost
        self.pet.attributes("-topmost",self.topmost)
        self.save()

    def toggle_demo(self):
        self.demo=not self.demo

    def preview(self,state):
        self.demo=True
        self.preview_state=state
        self.visual_key=None
        self.change_page("status")

    def choose_root(self,provider):
        path=filedialog.askdirectory(title=f"选择 {provider} 的 sessions / projects 目录",parent=self.root)
        if path:
            self.config[provider+"_root"]=path
            self.save()
            self.toast("目录已保存，重启应用后生效")

    def toast(self,message):
        self.notice=message
        self.notice_until=time.monotonic()+5

    def open_usage(self):
        s=self.current()
        url="https://claude.ai/settings/usage" if s and s.provider=="claude" else "https://chatgpt.com/codex/settings/usage"
        webbrowser.open(url)

    def open_help(self):
        messagebox.showinfo("PokeTask · 接入说明",
            "自动读取本机 Claude Code 与 Codex 的会话日志，每秒刷新。\n"
            "默认 ~/.claude/projects 与 ~/.codex/sessions；也支持环境变量和自定义目录。\n\n"
            "支持 Claude Code 本地 Code 会话与 VS Code 的 Claude Code/Codex 插件。"
            "云端、SSH、WSL 会话需指定可读取的本地日志目录。\n\n"
            "本轮完成表示 agent 结束响应，不代表测试或代码质量已通过。"
            "五分钟没有新工作日志显示待确认，不推断成功或失败。\n\n"
            "Codex 额度来自日志中最近一次上报，15 分钟后或过重置时间标旧；"
            "Claude 可在背包安装增强接入；只有状态栏实际提供额度时可显示，否则请打开官方用量页。\n\n"
            "不读取凭据、不上传对话。演示模式的数值是示例。\n"
            "点击桌宠展开，拖动移动，右键退出；Tab/Enter 操作面板。\n"
            "拖动顶部 POKETASK 移动菜单；收起或 Esc 隐藏。\n"
            "盒子可导入 ZIP 形象包、选择外观、导出巨金怪示例。\n\n"
            "询问状态的头顶气泡使用未知图腾 ?；失败/中断使用未知图腾 !。\n"
            "巨金怪与未知图腾使用原版像素图；彗星拳挥臂、星芒、举牌与气泡为本项目编排。"
            "道具仅用于菜单功能；字体：Fusion Pixel / OFL。",parent=self.root)

    def smoke_snapshot(self):
        target=Path(self.args.smoke_test)
        target.mkdir(parents=True,exist_ok=True)
        # Only application pixels are saved; the desktop and user logs are excluded.
        self.photo._PhotoImage__photo.write(str(target/"panel.png"),format="png")
        self.pet_photo._PhotoImage__photo.write(str(target/"pet.png"),format="png")
        original_page=self.page
        for page in ("usage","agents","settings","bridge","animations","box"):
            self.page=page
            self.draw_panel()
            self.photo._PhotoImage__photo.write(str(target/(page+".png")),format="png")
        self.page=original_page
        (target/"result.json").write_text(json.dumps({"ok":True,"sessions":len(self.sessions),
            "font":bool(self.art.font),"front_frames":len(self.art.frames["metagross"]),
            "selected_pet":self.selected_pet,"frameless":bool(self.root.overrideredirect()),
            "errors":self.errors,"observations":[{"provider":s.provider,"source":s.source,
                "state":s.state,"total_tokens":s.total_tokens,"quota_windows":len(s.quota)}
                for s in self.sessions]},ensure_ascii=False),encoding="utf-8")
        self.quit()

    def quit(self):
        self.save()
        self.stop.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser=argparse.ArgumentParser(description="PokeTask · 宝可梦桌宠")
    parser.add_argument("--demo",action="store_true")
    parser.add_argument("--smoke-test",metavar="OUTPUT_DIR")
    args=parser.parse_args()
    if sys.platform=="win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError,OSError):
            pass
        # A named mutex prevents duplicate watchers and duplicate notification sounds.
        mutex_name="Local\\PokeTaskStatus-"+hashlib.sha256(str(data_dir().resolve()).casefold().encode()).hexdigest()[:20]
        kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
        kernel.CreateMutexW.restype=ctypes.c_void_p
        mutex=kernel.CreateMutexW(None,False,mutex_name)
        if mutex and ctypes.get_last_error()==183:
            data_dir().mkdir(parents=True,exist_ok=True)
            (data_dir()/"show-panel.signal").touch()
            kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            kernel.CloseHandle(mutex)
            return
    try:
        app=App(args)
        app.run()
    except Exception as exc:
        if args.smoke_test:
            target=Path(args.smoke_test)
            target.mkdir(parents=True,exist_ok=True)
            (target/"failure.txt").write_text(type(exc).__name__+": "+str(exc),encoding="utf-8")
            raise
        try:
            tk.Tk().withdraw()
            messagebox.showerror("PokeTask 无法启动",f"{type(exc).__name__}: {exc}\n\n源码启动请先执行 python scripts/fetch_assets.py。")
        except Exception:
            pass
        raise
