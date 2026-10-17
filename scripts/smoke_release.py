"""Exercise the portable exe without project cwd, Python PATH, or real agents."""
import json
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    destination=ROOT/".local/release-smoke"
    destination.mkdir(parents=True,exist_ok=True)
    exe=destination/"PokeTaskStatus.exe"
    shutil.copy2(ROOT/"release/PokeTaskStatus.exe",exe)
    fixture=destination/"codex/sessions"
    fixture.mkdir(parents=True,exist_ok=True)
    now=time.time()
    records=[{"type":"session_meta","timestamp":now,"payload":{"id":"portable-fixture","source":"vscode","originator":"codex_vscode","cwd":"C:/Fixture/PixelLab"}},
             {"type":"event_msg","timestamp":now,"payload":{"type":"task_started"}},
             {"type":"event_msg","timestamp":now,"payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":1200,"output_tokens":80,"total_tokens":1280}},"rate_limits":{"primary":{"used_percent":23,"window_minutes":300,"resets_at":now+1000}}}}]
    (fixture/"session.jsonl").write_text("\n".join(json.dumps(record) for record in records)+"\n",encoding="utf-8")
    environment=os.environ.copy()
    environment.update(PATH=os.path.join(os.environ.get("SystemRoot","C:/Windows"),"System32"),
        PYTHONPATH="",PYTHONHOME="",CODEX_HOME=str(destination/"codex"),
        CLAUDE_CONFIG_DIR=str(destination/"claude"),POKETASK_DATA_DIR=str(destination/"data"))
    output=destination/("preview-"+str(time.time_ns()))
    subprocess.run([str(exe),"--smoke-test",str(output)],cwd=destination,env=environment,timeout=45,check=True)
    result=json.loads((output/"result.json").read_text(encoding="utf-8"))
    assert result["ok"] and result["font"] and result["front_frames"]>1 and not result["errors"],result
    assert result["sessions"]==1,result
    assert result["observations"][0]["total_tokens"]==1280,result
    assert result["observations"][0]["quota_windows"]==1,result
    for name in ("panel","pet","usage","agents","settings","bridge","animations"):
        assert (output/(name+".png")).is_file(),name
    # Verify the new input-request state in the packaged watcher and renderer.
    request={"type":"response_item","timestamp":time.time(),"payload":{
        "type":"function_call","name":"request_user_input","call_id":"portable-question",
        "arguments":"{}"}}
    with (fixture/"session.jsonl").open("a",encoding="utf-8") as stream:
        stream.write(json.dumps(request)+"\n")
    asking_output=destination/("asking-"+str(time.time_ns()))
    subprocess.run([str(exe),"--smoke-test",str(asking_output)],cwd=destination,env=environment,timeout=45,check=True)
    asking=json.loads((asking_output/"result.json").read_text(encoding="utf-8"))
    assert asking["ok"] and not asking["errors"] and asking["observations"][0]["state"]=="asking",asking
    assert asking["observations"][0]["total_tokens"]==1280,asking
    assert (asking_output/"pet.png").is_file()
    # A second double-click should reopen the existing app, not create a watcher.
    first_output=destination/("instance-first-"+str(time.time_ns()))
    second_output=destination/("instance-second-"+str(time.time_ns()))
    first=subprocess.Popen([str(exe),"--smoke-test",str(first_output)],cwd=destination,env=environment)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenMutexW.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.OpenMutexW.restype=ctypes.c_void_p
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    mutex_name="Local\\PokeTaskStatus-"+hashlib.sha256(str((destination/"data").resolve()).casefold().encode()).hexdigest()[:20]
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            handle=kernel.OpenMutexW(0x00100000,False,mutex_name)
            if handle:
                kernel.CloseHandle(handle)
                break
            time.sleep(.05)
        else:
            raise AssertionError("First instance did not acquire its mutex")
        subprocess.run([str(exe),"--smoke-test",str(second_output)],cwd=destination,env=environment,timeout=20,check=True)
        assert first.wait(timeout=20)==0
        assert (first_output/"result.json").is_file()
        assert not second_output.exists(),"Second launch created a duplicate GUI"
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
    print(json.dumps({"portable_smoke":"passed","python_path":"removed","working_directory":"isolated",
                      "single_instance":"passed","sha256":hashlib.sha256(exe.read_bytes()).hexdigest(),
                      "bytes":exe.stat().st_size,"result":result,"artifacts":str(output),
                      "asking_state":"passed","asking_artifacts":str(asking_output)},ensure_ascii=True))


if __name__=="__main__":
    main()
