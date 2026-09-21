#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务器 ASR 桥接 —— 本机与另一台带 GPU 的服务器之间的 ssh/scp 编排。

与 server_asr/asr.ps1 同一套命令序列，供「导入作品」页的服务器引擎使用：

    check_ssh → upload(stage) → start_transcribe(nohup) → wait_transcribe
    → pull(local_out) → cleanup

约定：
  - 本机侧所有 ssh/scp 都用 subprocess **列表参数**（不拼 shell 字符串，无注入面）；
  - 远端 shell 里的作品名/引擎/GPU 都经过调用方清洗（app.py 里 re.sub 非单词字符、
    本模块白名单 + 正则），组合出来的命令是安全的；
  - runner 可注入（测试用假 runner 返回预设 stdout/返回码），默认走 subprocess.run。

服务器是共享机，转写必须 nohup 后台跑 + pid 文件轮询（SSH 断开不中断）；
任务成功 cleanup 清全部残留，失败时保留转写产物与日志（供重试续跑/排查）。
"""
import json
import re
import subprocess
import time
from pathlib import Path

SERVER = ""
SRV_BASE = ""
ASR_CONFIG_PATH = Path(__file__).resolve().parent / "asr_server.json"


def _load_asr_config():
    """服务器账号与路径从同目录的 asr_server.json 读，不写进代码。

    这个模块文件是入库的，而 `user@内网IP` + 服务器上的工作目录属于本机配置：
    换机/换账号只改那个 json，不动代码，也不会把地址带进 git 历史。
    缺文件、缺字段都在这里不报错——只有真要连服务器时才出声（见 require_server）。
    """
    global SERVER, SRV_BASE
    SERVER = SRV_BASE = ""     # 先清再读：读不到就是没配置，不能留着上一次的值
    try:
        cfg = json.loads(ASR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    SERVER = str(cfg.get("server") or "").strip()
    SRV_BASE = str(cfg.get("base") or "").strip()


def require_server():
    """没配置就抛带指引的错，别让 ssh 拿着空地址跑出去。"""
    if not SERVER or not SRV_BASE:
        raise ServerAsrError(
            f"未配置 ASR 服务器：在 {ASR_CONFIG_PATH.name}（与 "
            "server_asr_bridge.py 同目录，不入 git）里写 "
            '{"server": "user@host", "base": "/path/to/asr"}')
    return SERVER, SRV_BASE


_load_asr_config()
SRV_JOBS = SRV_BASE + "/jobs" if SRV_BASE else "/jobs"
# 轮询节奏：一条 ssh 问 pid，问到 ALIVE 就睡 interval 再问
POLL_INTERVAL_SEC = 10
POLL_MAX_MISSES = 30  # 连续问不到（≈5 分钟）才放弃，单次抖动继续等
CONDA_SETUP = ("source /opt/anaconda3/etc/profile.d/conda.sh && "
               "conda activate asr")

# 面板能选的引擎（与 server_asr/transcribe.py --engines 一致）
ENGINE_WHITELIST = ("qwen3", "kotoba21", "kotoba22", "largev3")
GPU_RE = re.compile(r"^\d+(,\d+)*$")

# BatchMode=yes：免密才走（否则立即失败，避免网页流程被密码交互卡死）
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new"]


class ServerAsrError(RuntimeError):
    """桥接失败（VPN 不可达 / 免密缺失 / 命令失败 / 转写异常）。"""


def validate_engine(engine):
    """引擎名白名单校验 → 返回清洗后的引擎名。"""
    e = (engine or "").strip().lower()
    if e not in ENGINE_WHITELIST:
        raise ServerAsrError("不支持的引擎：" + str(engine)
                             + "（可选：" + ", ".join(ENGINE_WHITELIST) + "）")
    return e


def validate_gpu(gpu):
    """GPU 编号校验（`0,3` 逗号分隔正整数）。默认 fallback 到 "0"。"""
    g = (gpu or "").strip()
    if not g:
        return "0"
    if not GPU_RE.match(g):
        raise ServerAsrError(f"GPU 编号格式不对：{gpu!r}（应为 0 或 0,3）")
    return g


class ServerAsr:
    """SSH/SCP 编排；runner 可注入：runner(args: list[str]) -> CompletedProcess。"""

    def __init__(self, runner=None):
        self._runner = runner or self._default_runner

    # ---------- 底层 ----------

    def _default_runner(self, args, timeout=120):
        # 真要走 ssh/scp 了才检查配置：注入假 runner 的测试不需要服务器
        require_server()
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)

    def ssh(self, remote_cmd, timeout=120):
        """执行一条远端 shell 命令（remote_cmd 里可以安全拼接已清洗的名称）。"""
        return self._runner(["ssh", *SSH_OPTS, SERVER, remote_cmd],
                            timeout=timeout)

    def scp(self, args, timeout=1800):
        return self._runner(["scp", *SSH_OPTS, *args], timeout=timeout)

    def _ok(self, r, what):
        if r.returncode != 0:
            raise ServerAsrError(f"{what}失败："
                                 + (r.stderr or r.stdout or "").strip())

    # ---------- 步骤 ----------

    def check_ssh(self):
        """VPN / 免密快速探测；失败抛带指引的 ServerAsrError。"""
        try:
            r = self.ssh("exit 0", timeout=15)
        except Exception:
            raise ServerAsrError(
                f"无法连接服务器 {SERVER}：请确认已连上VPN，且 ssh 免密已配置"
                f"（ssh-copy-id {SERVER}）")
        if r.returncode != 0:
            raise ServerAsrError(
                f"无法连接服务器 {SERVER}：请确认已连上VPN，且 ssh 免密已配置"
                f"（ssh-copy-id {SERVER}）")
        return True

    def upload(self, stage_dir, srv_name):
        """scp 上传作品目录到 jobs/<srv_name>（scp 不自动建目录，先 mkdir）。"""
        self._ok(self.ssh(f"mkdir -p {SRV_JOBS}"), "创建服务器目录")
        self._ok(self.scp(["-r", str(stage_dir), f"{SERVER}:{SRV_JOBS}/"]),
                 "上传作品到服务器")

    def start_transcribe(self, srv_name, engine, gpu):
        """远端 nohup 后台转写，pid 写文件便于轮询。"""
        job_dir = f"{SRV_JOBS}/{srv_name}"
        cmd = (f"cd {SRV_BASE} && CUDA_VISIBLE_DEVICES={gpu} "
               f"python3 transcribe.py --audio {job_dir} "
               f"--out {job_dir}_out --engines {engine}")
        bg = (f"nohup bash -c '{CONDA_SETUP} && {cmd}' "
              f"> {job_dir}_asr.log 2>&1 & echo $! > {job_dir}_asr.pid")
        self._ok(self.ssh(bg), "启动服务器转写")

    def wait_transcribe(self, srv_name, max_sec=7200, interval=POLL_INTERVAL_SEC):
        """轮询 pid 直到进程退出（每 interval 秒一条 ssh）。返回日志末尾。

        远端是 nohup 后台跑，本地 ssh 抖动只当「这一轮没问到」，连续失败够多轮
        才放弃——中途 break 会被上层当成转写失败，连带把远端产物 cleanup 掉。
        """
        pid_file = f"{SRV_JOBS}/{srv_name}_asr.pid"
        waited = 0
        misses = 0
        while waited < max_sec:
            try:
                r = self.ssh(
                    f"kill -0 `cat {pid_file} 2>/dev/null` 2>/dev/null"
                    " && echo ALIVE || echo DEAD", timeout=30)
                misses = 0
            except Exception:
                misses += 1
                if misses >= POLL_MAX_MISSES:
                    raise ServerAsrError(
                        f"连续 {misses} 次问不到服务器状态（转写还在远端跑），"
                        f"可稍后重新提交")
                time.sleep(interval)
                waited += interval
                continue
            if r.returncode == 0 and "ALIVE" in r.stdout:
                time.sleep(interval)
                waited += interval
                continue
            # 进程已退出：读日志确认结果
            log = self.ssh(f"tail -n 20 {SRV_JOBS}/{srv_name}_asr.log",
                           timeout=30)
            return log.stdout.strip()
        raise ServerAsrError(
            f"服务器转写超过 {max_sec // 60} 分钟仍未结束，可稍后重新提交")

    def pull(self, srv_name, local_out):
        """scp 拉回 <srv_name>_out/ 到本地。"""
        out_dir = Path(local_out)
        out_dir.mkdir(parents=True, exist_ok=True)
        self._ok(self.scp(
            ["-r", f"{SERVER}:{SRV_JOBS}/{srv_name}_out/", f"{out_dir}/"]),
            "拉回服务器结果")

    def cleanup(self, srv_name, keep_results=False):
        """清服务器残留（best-effort，失败不抛）。

        keep_results=True 用于任务失败时：转写产物 `_out` 与日志留着（纯文本，
        体积小），便于重新提交时直接续跑或人工排查；上传的音轨（体积大头）照删。
        """
        if not srv_name or "/" in srv_name or "\\" in srv_name:
            # 空名字会把目标退化成 `rm -rf <jobs 根目录>`——连别人正在跑的任务
            # 一起删掉；带分隔符则跳出 jobs/。两处调用都包了 except，抛错即跳过。
            raise ServerAsrError(f"非法的服务器任务名：{srv_name!r}")
        job_dir = f"{SRV_JOBS}/{srv_name}"
        targets = (f"{job_dir} {job_dir}_asr.pid" if keep_results else
                   f"{job_dir} {job_dir}_out "
                   f"{job_dir}_asr.log {job_dir}_asr.pid")
        try:
            self.ssh(f"rm -rf {targets}", timeout=60)
        except Exception:
            pass

    def gpu_status(self):
        """服务器 nvidia-smi → [{index,name,mem_total,mem_used,util}]（GiB）。"""
        try:
            r = self._runner(
                ["ssh", *SSH_OPTS, SERVER,
                 "nvidia-smi --query-gpu=index,name,memory.total,memory.used,"
                 "utilization.gpu --format=csv,noheader,nounits"],
                timeout=30)
        except Exception:
            raise ServerAsrError(
                "无法连接服务器：请确认已连上VPN，且 ssh 免密已配置")
        if r.returncode != 0:
            raise ServerAsrError("服务器 nvidia-smi 查询失败："
                                 + (r.stderr or r.stdout or "").strip())
        gpus = []
        for line in (r.stdout or "").splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue  # 下面取到 parts[4]：只挡 4 个字段会在这里 IndexError 炸成 500
            try:
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "mem_total": round(int(parts[2]) / 1024, 1),
                    "mem_used": round(int(parts[3]) / 1024, 1),
                    "util": int(parts[4]),
                })
            except ValueError:
                continue
        return gpus

    def load_engine_segs(self, local_out, engine):
        """<local_out>/<engine>/*.json → {主名: [{t0,t1,text}]}。

        主名用 import_voice.norm_stem 口径归一，与本地轨道 label 对齐。
        """
        from import_voice import norm_stem
        segs_by_stem = {}
        base = Path(local_out) / engine
        if not base.is_dir():
            return segs_by_stem
        for f in sorted(base.glob("*.json")):
            if f.name in ("meta.json",) or not f.is_file():
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, list):
                continue
            stem = norm_stem(f.name)
            segs_by_stem.setdefault(stem, []).extend(data)
        return segs_by_stem