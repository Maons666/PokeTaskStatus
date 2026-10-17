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
        self.root.title("PokeTask · 巨金怪图鉴")
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
        self.canvas.bind("<Button-1>", self.click)
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.root.bind("<Tab>", self.focus_next)
        self.root.bind("<Return>", self.activate_focused)
        self.root.bind("<F1>", lambda e: self.open_help())
        self.buttons = []
        self.focused = -1
        self.page = "status"
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
        self.bridge_health=(False,False)
        self.bridge_health_at=0
        self.panel_visible = False
        self.sound = self.config.get("sound", True) is not False
        self.topmost = self.config.get("topmost", True) is not False
        self.pet = tk.Toplevel(self.root)
        self.pet.title("PokeTask · 巨金怪")
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
        self.root.geometry(f"+{max(0,(sw-self.W*self.scale)//2)}+{max(0,(sh-self.H*self.scale)//2-30)}")
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
            return Session("demo", "codex", "VS Code", "演示训练场",state=self.preview_state,
                updated_at=time.time(),input_tokens=18920,output_tokens=2840,cached_tokens=12200,
                total_tokens=21760,context_percent=23.0,
                quota=[QuotaWindow("5 小时",32,time.time()+7200),QuotaWindow("每周",18,time.time()+86400)],
                detail="演示数据 · 不代表真实 agent 状态")
        return next((s for s in self.sessions if self.key(s)==self.selected),None)

    def tick(self):
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
                    self.toast("巨金怪：当前 agent 本轮已完成")
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
        key=("demo" if self.demo else self.selected,state)
        if key!=self.visual_key:
            self.visual_key=key
            self.visual_started=time.monotonic()
        return time.monotonic()-self.visual_started

    def draw_panel(self):
        im=Image.new("RGBA",(self.W,self.H),RED)
        d=ImageDraw.Draw(im)
        d.rectangle((3,3,316,398),outline=INK,width=2)
        d.rectangle((8,8,311,39),fill=INK)
        self.art.item(im,"poke-ball",9,8)
        self.art.text(im,(42,13),"POKETASK / 巨金怪",PAPER)
        self.buttons=[]
        self.button(im,(265,12,36,19),"收起",self.hide_panel)
        for i,(label,page,icon) in enumerate((("图鉴","status","poke-radar"),("队伍","agents","poke-ball"),("背包","settings","silph-scope"))):
            self.button(im,(12+i*101,47,94,23),label,lambda p=page:self.change_page(p),self.page==page,icon=icon)
        if self.page=="status":
            self.draw_status(im)
        elif self.page=="agents":
            self.draw_agents(im)
        else:
            self.draw_settings(im)
        text=self.notice if time.monotonic()<self.notice_until else ("演示模式 · 不是真实会话" if self.demo else self.errors[0] if self.errors else "本地只读 · 单击桌宠展开 · 右键菜单")
        self.art.text(im,(12,381),self.art.fit(text,296),INK)
        self.photo=ImageTk.PhotoImage(im.convert("RGB").resize((self.W*self.scale,self.H*self.scale),Image.Resampling.NEAREST))
        self.canvas.itemconfigure(self.surface,image=self.photo)

    def draw_status(self,im):
        s=self.current()
        state=s.state if s else "disconnected"
        name,color=STATES.get(state,STATES["unknown"])[0::2]
        panel_box(im,(12,79,296,38))
        provider=("CODEX" if s.provider=="codex" else "CLAUDE") if s else "AGENT"
        self.art.item(im,"poke-radar",17,83)
        self.art.text(im,(52,84),provider+" / "+(s.source if s else "未发现会话"))
        self.art.text(im,(52,101),self.art.fit(s.name+" · "+s.id[:8] if s else "打开 agent 开始任务，或进入背包",246),GREEN)
        animation="idle" if s and state=="completed" and not self.demo and time.time()-s.updated_at>4 else state
        self.art.scene(im,(14,126,292,104),animation,self.animation_time(state))
        panel_box(im,(12,233,296,33))
        ImageDraw.Draw(im).rectangle((20,242,25,247),fill=color)
        self.art.text(im,(31,237),name+(" · 演示" if self.demo else ""))
        age=max(0,int(time.time()-s.updated_at)) if s else None
        self.art.text(im,(190,237),f"{age//60}分钟前" if age is not None and age>=60 else ("刚刚更新" if s else "等待连接"),GREEN)
        detail=(s.detail or STATES[state][1]) if s else "可自动发现 Claude Code / Codex 本地会话"
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
            self.art.text(im,(20,299),"未知 · 当前来源未提供账户限额")
            self.art.text(im,(20,317),"在用量详情中打开官方用量页",GREEN)
        self.button(im,(12,345,145,25),"代币盒 / 用量",lambda:self.change_page("usage"),icon="coin-case")
        self.button(im,(164,345,144,25),"声音："+("开" if self.sound else "关"),self.toggle_sound,icon="poke-flute")

    def draw_agents(self,im):
        panel_box(im,(12,80,296,41))
        self.art.item(im,"poke-ball",15,83)
        self.art.text(im,(49,86),f"精灵球队伍 · {len(self.sessions)} 个会话")
        self.art.text(im,(49,104),"手动选择 · 历史会话可能已退出",GREEN)
        if not self.sessions:
            self.art.text(im,(20,151),"尚未发现会话。")
            self.art.text(im,(20,173),"先启动 Claude Code 或 Codex。")
            self.art.text(im,(20,195),"WSL / 自定义目录：在背包中选择。")
        self.offset=max(0,min(self.offset,max(0,len(self.sessions)-4)))
        for i,s in enumerate(self.sessions[self.offset:self.offset+4]):
            y=130+i*49
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
        panel_box(im,(12,80,296,39))
        self.art.item(im,"silph-scope",16,83)
        self.art.text(im,(49,85),"训练家背包")
        self.art.text(im,(49,102),"使用道具，管理你的桌宠",GREEN)
        tiles=[("宝可梦之笛","声音："+("开" if self.sound else "关"),"poke-flute",self.toggle_sound),
               ("金属膜","置顶："+("开" if self.topmost else "关"),"metal-coat",self.toggle_top),
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
        panel_box(im,(12,80,296,40))
        self.art.item(im,"vs-recorder",16,84)
        self.art.text(im,(49,86),"对战记录器 / 动画预览")
        self.art.text(im,(49,104),"选择动作后进入演示，不改真实状态",GREEN)
        states=(("巡逻","idle"),("彗星拳","working"),("询问 ?","asking"),
                ("举牌完成","completed"),("普通等待","waiting"),("中断 !","interrupted"),
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
        panel_box(im,(12,80,296,249))
        self.art.text(im,(22,91),"Claude 增强接入",GREEN)
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
            self.art.text(im,(22,119+i*19),line)
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
        panel_box(im,(12,80,296,249))
        self.art.item(im,"coin-case",15,84)
        self.art.text(im,(49,87),"代币盒 / 会话 TOKEN")
        if s:
            rows=[("累计输入",number(s.input_tokens)),("其中缓存读取",number(s.cached_tokens)),
                  ("累计输出",number(s.output_tokens)),("累计总量",number(s.total_tokens)),
                  ("最近上下文占用",f"{s.context_percent:.1f}%" if s.context_percent is not None else "未知")]
            for i,(label,value) in enumerate(rows):
                self.art.text(im,(22,123+i*23),label,GREEN)
                self.art.text(im,(207,123+i*23),value)
            self.art.text(im,(22,244),"统计范围："+("正在载入历史，暂不完整" if s.usage_partial else "本地可读取的本次会话"),GREEN)
            self.art.text(im,(22,263),"缓存已计入输入；token 不是账户余额")
            for i,q in enumerate(s.quota[:2]):
                try:
                    reset=time.strftime("%m-%d %H:%M",time.localtime(q.resets_at)) if q.resets_at else "未知"
                except (ValueError,OverflowError,OSError):
                    reset="未知"
                self.art.text(im,(22,285+i*17),self.art.fit(q.label+" 重置："+reset,274))
        else:
            self.art.text(im,(22,133),"请先在队伍中选择一个会话。")
        self.button(im,(12,344,145,26),"官方用量页面",self.open_usage)
        self.button(im,(164,344,144,26),"返回图鉴",lambda:self.change_page("status"))

    def draw_pet(self):
        im=Image.new("RGBA",(PET_WIDTH,PET_HEIGHT),TRANSPARENT_KEY)
        s=self.current()
        state=s.state if s else "disconnected"
        animation="idle" if s and state=="completed" and not self.demo and time.time()-s.updated_at>4 else state
        self.art.scene(im,(0,0,PET_WIDTH,PET_HEIGHT),animation,self.animation_time(state),
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

    def shift(self,delta):
        self.offset=max(0,min(max(0,len(self.sessions)-4),self.offset+delta))

    def click(self,event):
        x,y=event.x/self.scale,event.y/self.scale
        for (bx,by,w,h),action in self.buttons:
            if bx<=x<=bx+w and by<=y<=by+h:
                action()
                return

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
        menu.add_command(label="打开巨金怪图鉴",command=self.show_panel)
        menu.add_command(label="声音："+("开" if self.sound else "关"),command=self.toggle_sound)
        menu.add_command(label="退出 PokeTask",command=self.quit)
        menu.tk_popup(event.x_root,event.y_root)

    def show_panel(self):
        self.panel_visible=True
        self.root.deiconify()
        self.root.lift()

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
            "点击桌宠展开，拖动移动，右键退出；Tab/Enter 操作面板。\n\n"
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
        for page in ("usage","agents","settings","bridge","animations"):
            self.page=page
            self.draw_panel()
            self.photo._PhotoImage__photo.write(str(target/(page+".png")),format="png")
        self.page=original_page
        (target/"result.json").write_text(json.dumps({"ok":True,"sessions":len(self.sessions),
            "font":bool(self.art.font),"front_frames":len(self.art.frames["metagross"]),
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
    parser=argparse.ArgumentParser(description="PokeTask · 巨金怪桌宠")
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
