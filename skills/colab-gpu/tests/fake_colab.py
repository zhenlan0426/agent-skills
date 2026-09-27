#!/usr/bin/env python3
"""A stand-in for colab-cli 0.7.4, faithful where cgpu depends on it.

State lives in $FAKE_COLAB. Each VM is a directory whose `fs/` is the VM's `/`
(cell code has its '/content', '/root' and '/tmp/cgpu-*' paths rewritten
into it).
The kernel is FIFO: `exec` hands its cell to a detached worker that waits on
the VM's kernel lock, so a queued cell still runs after its client is killed,
as on the real service. Messages copy 0.7.4's wording, which cgpu matches on.
Every invocation is appended to $FAKE_COLAB/calls.log.
"""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ["FAKE_COLAB"])
MAP = ROOT / "sessions.json"  # local name -> VM id, as colab-cli keeps it


def names():
    return json.loads(MAP.read_text()) if MAP.exists() else {}


def vm_dir(session):
    vm = names().get(session)
    if vm is None or not (ROOT / "vms" / vm).exists():
        print(f"[colab] Session '{session}' not found.")
        sys.exit(1)
    return ROOT / "vms" / vm


def remote(vm, path):
    return vm / "fs" / path.strip("/")


def rewrite(vm, code):
    # Only paths cgpu itself uses under /tmp, so a test's own temp dir survives.
    for prefix in ("content", "root", "tmp/cgpu-"):
        code = code.replace(f"'/{prefix}", f"'{vm / 'fs'}/{prefix}")
    return code


def opt(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def positional(args):
    out, skip = [], False
    for x in args:
        if skip:
            skip = False
        elif x in ("-s", "--gpu", "--timeout", "-f"):
            skip = True
        elif not x.startswith("-"):
            out.append(x)
    return out


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--auth=")]
    with open(ROOT / "calls.log", "a") as f:
        f.write(json.dumps(args) + "\n")
    cmd, rest = args[0], args[1:]
    s = opt(rest, "-s")
    if cmd == "version":
        print("Version: 0.7.4")
    elif cmd == "new":
        vm = uuid.uuid4().hex[:8]
        for top in ("content", "tmp", "root"):
            (ROOT / "vms" / vm / "fs" / top).mkdir(parents=True)
        m = names()
        m[s] = vm  # 0.7.4 overwrites an existing name's mapping
        MAP.write_text(json.dumps(m))
        with open(ROOT / "allocations.log", "a") as f:
            f.write(f"{s} {vm}\n")
        print(f"[colab] Creating session '{s}'...")
    elif cmd == "sessions":
        m = {k: v for k, v in names().items() if (ROOT / "vms" / v).exists()}
        MAP.write_text(json.dumps(m))  # prune, like sync_sessions
        by_vm = {v: k for k, v in m.items()}
        for vm in sorted(p.name for p in (ROOT / "vms").glob("*")):
            print(f"[{by_vm.get(vm, '?')}] ep-{vm} | Hardware: GPU T4 | "
                  "Shape: Standard | Variant: GPU")
    elif cmd == "stop":
        shutil.rmtree(vm_dir(s))
        m = names()
        m.pop(s, None)
        MAP.write_text(json.dumps(m))
    elif cmd == "usage":
        print("[colab] usage: 100 units")
    elif cmd == "upload":
        vm = vm_dir(s)
        src, dst = positional(rest)
        target = remote(vm, dst)
        if not target.parent.is_dir():
            print(f"[colab] Upload failed: 404 no such directory: {dst}")
            sys.exit(1)
        shutil.copyfile(src, target)
        print(f"[colab] Uploaded '{src}' to '{dst}'")
    elif cmd == "download":
        vm = vm_dir(s)
        src, dst = positional(rest)
        path = remote(vm, src)
        try:
            if not path.exists():
                raise FileNotFoundError(f"File or directory not found: {src}")
            if path.is_dir():
                raise IsADirectoryError(f"Cannot download a directory: {src}")
            data = path.read_bytes()
            if os.environ.get("FAKE_COLAB_DOWNLOAD_FAIL"):
                with open(dst, "wb") as f:
                    f.write(data[: len(data) // 2])
                raise ConnectionError("connection reset mid-transfer")
            with open(dst, "wb") as f:
                f.write(data)
        except Exception as e:
            print(f"[colab] Download failed: {e}")
            sys.exit(1)
        print(f"[colab] Downloaded '{src}' to '{dst}'")
    elif cmd == "rm":
        vm = vm_dir(s)
        p = remote(vm, positional(rest)[0])
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    elif cmd == "exec":
        vm = vm_dir(s)
        cell = vm / f"cell-{uuid.uuid4().hex[:8]}.py"
        cell.write_text(rewrite(vm, Path(opt(rest, "-f")).read_text()))
        out = cell.with_suffix(".out")
        out.touch()
        worker = subprocess.Popen(
            [sys.executable, __file__, "_kernel", str(vm), str(cell)],
            stdout=subprocess.DEVNULL, start_new_session=True)
        (vm / "workers").mkdir(exist_ok=True)
        (vm / "workers" / str(worker.pid)).touch()
        deadline = time.time() + float(opt(rest, "--timeout", "30"))
        pos = 0
        while True:
            done = cell.with_suffix(".done").exists()
            with open(out) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            sys.stdout.write(chunk)
            sys.stdout.flush()
            if done:
                break
            if time.time() > deadline:
                print("TimeoutError: Execution timed out")
                sys.exit(1)
            time.sleep(0.1)
    elif cmd == "_kernel":  # the worker: run one cell once the kernel is free
        vm, cell = Path(rest[0]), Path(rest[1])
        with open(vm / "kernel.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(cell.with_suffix(".out"), "w") as out:
                subprocess.run([sys.executable, str(cell)], stdout=out,
                               stderr=subprocess.STDOUT)
        cell.with_suffix(".done").touch()
    elif cmd == "restart-kernel":
        vm = vm_dir(s)
        for w in (vm / "workers").glob("*"):
            try:
                os.killpg(int(w.name), signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif cmd == "console":
        vm_dir(s)
        subprocess.run(["bash"], stdin=sys.stdin)
    else:
        print(f"fake colab: unsupported command {cmd}")
        sys.exit(2)


if __name__ == "__main__":
    main()
