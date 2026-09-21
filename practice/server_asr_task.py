#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务器 ASR 导入任务 —— 落盘状态机 + 后台 worker。

「导入作品」选服务器引擎时，POST 立即返回 task_id，worker 线程按
collecting → uploading → transcribing → pulling → aligning → importing → done
推进。每步 update_task 都会把任务写盘到 data/import_tasks/<id>.json：
本地 Flask 关了，服务器转写（远端 nohup）照跑；重启时 restore_tasks() 扫盘，
把 transcribing 之后的任务恢复线程接着跑（scp 拉回/导入幂等）。

worker 只读文件路径与任务表，**绝不碰 Flask request**（POST 内已把文件
落到 data/audio/<key>/，线程拿到的是纯路径）。
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import works  # noqa: E402 （取 WORKS_DIR 算出 data/ 根）

# 任务落盘目录：works.WORKS_DIR 的父目录下 import_tasks/（即 data/import_tasks/）
TASKS_DIR = Path(works.WORKS_DIR).parent / "import_tasks"

_LOCATIONS = {
    "collecting": "准备中", "uploading": "上传音轨到服务器",
    "transcribing": "服务器转写中", "pulling": "拉回转写结果",
    "aligning": "组装时间轴并导入", "importing": "写入作品",
    "done": "完成", "error": "失败",
}
_PHASE_ORDER = [k for k in _LOCATIONS if k not in ("done", "error")]

_TASKS = {}
_LOCK = threading.Lock()

# 重启恢复：从这些阶段起，worker 可以继续跑（远端转写/拉回/导入都是幂等的）
_RESUMABLE = {"transcribing", "pulling", "aligning", "importing"}


class TaskError(RuntimeError):
    pass


@dataclass
class AsrTask:
    id: str
    phase: str = "collecting"
    msg: str = "准备中"
    error: str = ""
    title: str = ""
    author: str = ""
    engine: str = "qwen3"
    gpu: str = "0"
    key: str = ""            # 服务器 jobs/<key> 作品名（= data/audio/<key>）
    stage: str = ""          # 本地 data/audio/<key> 绝对路径
    work_id: str = ""
    chapters: int = 0
    sentences: int = 0
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    done: bool = False

    def to_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def _path(tid):
    return TASKS_DIR / f"{tid}.json"


def create_task(stage, title="", author="", engine="qwen3", gpu="0", key=""):
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    t = AsrTask(id=uuid.uuid4().hex[:12], title=title, author=author,
                engine=engine, gpu=gpu, key=key, stage=str(stage))
    with _LOCK:
        _TASKS[t.id] = t
    _persist(t)
    return t


def _persist(t):
    """任务状态落盘。写失败必须出声：只剩内存副本的话，重启后
    `restore_tasks` 找不到它——远端产物既不拉回也不清理，前端也再看不到进度。
    """
    try:
        _path(t.id).write_text(json.dumps(t.to_dict(), ensure_ascii=False),
                               encoding="utf-8")
    except OSError as e:
        print(f"[asr] 任务 {t.id} 状态落盘失败（杀软/OneDrive 锁文件？）：{e}")


def update_task(tid, **kw):
    with _LOCK:
        t = _TASKS.get(tid)
    if t is None:
        t = _load(tid)
        if t is None:
            raise KeyError(tid)
        with _LOCK:
            _TASKS[tid] = t
    for k, v in kw.items():
        setattr(t, k, v)
    t.updated = time.time()
    _persist(t)
    return t


def get_task(tid):
    with _LOCK:
        t = _TASKS.get(tid)
    if t is not None:
        return t
    return _load(tid)


def _load(tid):
    p = _path(tid)
    if not p.exists():
        return None
    try:
        t = AsrTask.from_dict(json.loads(p.read_text(encoding="utf-8")))
        with _LOCK:
            _TASKS[t.id] = t
        return t
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def active_tasks(limit=5):
    """最近活跃任务（未 done 优先，其次按 updated 倒序）。"""
    out = []
    for tid in sorted(_path_base_glob(), reverse=True)[:50]:
        t = _load(tid)
        if t is not None:
            out.append(t)
    out.sort(key=lambda t: (0 if not t.done else 1, -t.updated))
    return out[:limit]


def _path_base_glob():
    if not TASKS_DIR.is_dir():
        return []
    # done 超过 24h 顺手清掉，避免目录无限膨胀
    stale = time.time() - 24 * 3600
    for p in TASKS_DIR.glob("*.json"):
        try:
            t = AsrTask.from_dict(json.loads(p.read_text(encoding="utf-8")))
            if t.done and t.updated < stale:
                p.unlink()
        except Exception:
            pass
    return [p.stem for p in TASKS_DIR.glob("*.json")]


def restore_tasks():
    """Flask 启动时扫盘恢复未完成任务。

    transcribing 之后 → 返回任务（调用方为其起继续线程）；
    collecting/uploading（本地中断，上传可能半途）→ 标 error，并清远程残留。
    """
    bridge = None
    restored = []
    if not TASKS_DIR.is_dir():
        return restored
    for p in TASKS_DIR.glob("*.json"):
        try:
            t = AsrTask.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
        if t.done:
            continue
        if t.phase in _RESUMABLE:
            with _LOCK:
                _TASKS[t.id] = t
            restored.append(t)
        else:
            # 本地中断（收集/上传阶段）：无法安全续跑
            t.error = "Flask 在收集/上传阶段中断，请重新提交导入"
            t.phase = "error"
            t.done = True
            with _LOCK:
                _TASKS[t.id] = t      # 内存与盘一致：get_task 别拿到过期对象
            _persist(t)
            try:
                from server_asr_bridge import ServerAsr
                bridge = bridge or ServerAsr()
                bridge.cleanup(t.key)
            except Exception:
                pass
    return restored


# ---------- worker ----------

def _local_out(stage):
    """服务器结果拉回本地目录：data/audio/<key>/_asr_out/。"""
    return str(Path(stage) / "_asr_out")


def _segs_map(local_out, engine):
    from server_asr_bridge import ServerAsr
    return ServerAsr().load_engine_segs(local_out, engine)


def _do_import(t, segs):
    """把服务器时间轴喂给 import_voice（有台本→对齐；无文本→直接当句子）。"""
    from import_voice import import_voice, norm_stem
    try:
        meta = import_voice(
            root=t.stage, title=t.title, author=t.author,
            align=True, model=f"server:{t.engine}",
            seg_provider=lambda trk, s=segs: s.get(norm_stem(trk.label)),
            probe=False)
    except SystemExit as e:
        raise TaskError(str(e))
    chapters = meta.get("chapters", [])
    return {
        "work_id": meta.get("id", ""),
        "chapters": len(chapters),
        "sentences": sum(c.get("sentences", 0) for c in chapters),
    }


def run_task(task_id, bridge=None):
    """推进单个任务到 done/error。bridge 可注入（测试 mock）。"""
    if bridge is None:
        from server_asr_bridge import ServerAsr, ServerAsrError
        bridge = ServerAsr()
    else:
        from server_asr_bridge import ServerAsrError

    t = get_task(task_id)
    if t is None:
        return
    ok = False  # 只有走到「导入完成」才清干净服务器残留
    try:
        if t.phase in _RESUMABLE:
            # 续跑：transcribing 从轮询起；pulling/aligning/importing 从拉回起
            phase = t.phase
        else:
            phase = "collecting"

        if phase in ("collecting", "uploading", "transcribing"):
            if phase in ("collecting", "uploading"):
                update_task(task_id, phase="collecting", msg="检查音轨清单…")
                if not _discover_any(t.stage):
                    raise TaskError("作品目录里没有可转写的音轨（支持 mp3/m4a/"
                                    "aac/opus/ogg/flac/wav）")
                update_task(task_id, phase="uploading", msg="连接服务器…")
                bridge.check_ssh()
                update_task(task_id, phase="uploading",
                            msg=f"上传音轨到服务器（{t.engine}）…")
                bridge.upload(t.stage, t.key)
                update_task(task_id, phase="transcribing",
                            msg=f"服务器转写中（{t.engine}，卡 {t.gpu}）…")
                bridge.start_transcribe(t.key, t.engine, t.gpu)
            else:  # 恢复：转写已在远端 nohup 跑着，继续轮询
                update_task(task_id, msg=f"服务器转写中（{t.engine}，卡 {t.gpu}）…")
            bridge.wait_transcribe(t.key)

        update_task(task_id, phase="pulling", msg="拉回转写结果…")
        bridge.pull(t.key, _local_out(t.stage))
        segs = _segs_map(_local_out(t.stage), t.engine)
        if not segs:
            raise TaskError("服务器没有返回转写结果（检查引擎/音轨格式）")

        update_task(task_id, phase="aligning", msg="组装时间轴并导入…")
        result = _do_import(t, segs)

        update_task(task_id, phase="done", msg="导入完成", done=True,
                    work_id=result["work_id"], chapters=result["chapters"],
                    sentences=result["sentences"], error="")
        ok = True
    except TaskError as e:
        _fail(task_id, str(e))
    except ServerAsrError as e:
        _fail(task_id, str(e))
    except Exception as e:  # noqa: BLE001 —— worker 兜底，任何异常都要落成 error
        import traceback
        traceback.print_exc()
        _fail(task_id, f"导入异常：{e}")
    finally:
        try:
            # 失败别把远端结果删了：转写可能已经跑完（甚至还在跑），
            # 留着 _out 与日志才能「重新提交」直接续跑或人工排查
            bridge.cleanup(t.key, keep_results=not ok)
        except Exception:
            pass


def _fail(task_id, msg):
    try:
        update_task(task_id, phase="error", msg=msg, error=msg, done=True)
    except KeyError:
        pass


def _discover_any(stage):
    """作品目录里至少要有一轨音轨（早失败，省一次上传流量）。"""
    from import_voice import AUDIO_EXTS
    root = Path(stage)
    if not root.is_dir():
        return False
    return any(p.suffix.lower() in AUDIO_EXTS
               for p in root.rglob("*") if p.is_file())