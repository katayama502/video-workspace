"""
editor.py — 動画手動編集エディタ（Web ベース）
────────────────────────────────────────────────
使い方:
  python editor.py
  → ブラウザで http://localhost:8080 を開く

機能:
  - 自動編集後の字幕テキスト・タイミングを手動修正
  - 強調テロップ ON/OFF の切り替え
  - BGM / SE / エンドカード の設定変更
  - サムネイルフレームの手動選択
  - 編集内容を保存して再レンダリング（ffmpeg パイプライン再実行）
  - エクスポートの進捗をリアルタイム表示
"""

import os
import re
import json
import shutil
import textwrap
import threading
from pathlib import Path

WORKSPACE  = Path(__file__).parent
STATE_FILE = WORKSPACE / "output" / "edit_state.json"
FFMPEG = (
    "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    if os.path.exists("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
    else "ffmpeg"
)

# ──────────────────────────────────────────────────
# 起動時に fastapi / uvicorn を自動インストール
# ──────────────────────────────────────────────────
def _ensure_deps():
    import subprocess, sys
    pkgs = []
    try:
        import fastapi
    except ImportError:
        pkgs.append("fastapi")
    try:
        import uvicorn
    except ImportError:
        pkgs.append("uvicorn[standard]")
    try:
        import python_multipart  # noqa
    except ImportError:
        pkgs.append("python-multipart")
    if not pkgs:
        return
    print(f"📦 必要なパッケージをインストール中: {pkgs}")
    # --break-system-packages を付けてシステムPythonでも動くようにする
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q",
         "--break-system-packages"] + pkgs,
        capture_output=True,
    )
    if result.returncode != 0:
        # フォールバック: --user インストール
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q",
             "--user"] + pkgs,
            check=True,
        )

_ensure_deps()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import (                      # noqa: E402
    FileResponse, HTMLResponse, JSONResponse
)
from fastapi.middleware.cors import CORSMiddleware   # noqa: E402
import uvicorn                                       # noqa: E402

app = FastAPI(title="動画エディタ")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ──────────────────────────────────────────────────
# 状態管理
# ──────────────────────────────────────────────────

DEFAULT_STATE = {
    "source_video":  "",          # 編集元動画パス（WORKSPACE からの相対）
    "subtitles":     [],          # [{id, start, end, text, emphasis}, ...]
    "cut_silence":   True,
    "bgm": {
        "enabled": True,
        "file":    "bgm/Rain.mp3",
        "volume":  0.12,
    },
    "se": {
        "enabled": False,
        "file":    "",
        "events":  [],            # [{time, volume}, ...]
    },
    "end_card": {
        "enabled":  True,
        "text":     "チャンネル登録よろしく！",
        "subtext":  "↑ 高評価もお願いします",
        "duration": 5,
    },
    "thumbnail": {
        "enabled":   True,
        "title":     "",
        "seek_time": 30.0,
    },
    "style_template": {
        "preset":         "default",
        "font_family":    "gothic_black",
        "font_size":      52,
        "color":          "#ffffff",
        "outline_color":  "#000000",
        "outline_width":  0,
        "bg_enabled":     True,
        "bg_color":       "black",
        "bg_opacity":     0.65,
        "bg_padding":     10,
        "animation":      "fade",
        "pos_y":          85,
        "emphasis_color": "#f5c518",
    },
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def parse_srt(srt_path: str) -> list:
    if not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    segs = []
    for i, block in enumerate(re.split(r"\n\n+", content.strip())):
        lines = block.strip().splitlines()
        tc = next((l for l in lines if "-->" in l), None)
        if not tc:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", tc
        )
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start = h1*3600 + m1*60 + s1 + ms1/1000
        end   = h2*3600 + m2*60 + s2 + ms2/1000
        ti    = lines.index(tc)
        text  = " ".join(lines[ti + 1:]).strip()
        segs.append({"id": i, "start": start, "end": end, "text": text, "emphasis": False})
    return segs


# ──────────────────────────────────────────────────
# API エンドポイント
# ──────────────────────────────────────────────────

@app.get("/api/state")
def get_state():
    state = load_state()

    # 字幕: SRT から自動取得（まだ state に入っていない場合）
    if not state.get("subtitles"):
        for srt_cand in ["output/02_subtitled.srt", "output/editor_subtitles.srt"]:
            p = WORKSPACE / srt_cand
            if p.exists():
                state["subtitles"] = parse_srt(str(p))
                break

    # ソース動画: 字幕焼き込み前の動画を優先（二重表示防止）
    # エディタは「字幕なし」動画をプレビューし、JS側で字幕をオーバーレイする
    if not state.get("source_video"):
        # input/ フォルダ内の動画を動的に探す
        input_videos = sorted(
            (WORKSPACE / "input").glob("*"),
            key=lambda f: f.stat().st_mtime if f.exists() else 0,
            reverse=True,
        )
        input_candidates = [
            f"input/{f.name}" for f in input_videos
            if f.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
        ]

        for cand in [
            "output/editor_source.mp4",  # run.py が字幕焼き前に保存した版（最優先）
            *input_candidates,           # input/ フォルダの元動画
            "output/01_generated.mp4",   # AI生成動画（字幕なし）
            "output/final_16x9.mp4",     # 最終出力（字幕焼き込み済み、最後の手段）
        ]:
            if (WORKSPACE / cand).exists():
                state["source_video"] = cand
                break

    return state


@app.post("/api/state")
async def post_state(request: Request):
    state = await request.json()
    save_state(state)
    return {"ok": True}


@app.get("/api/files")
def list_files():
    """BGM・SE・動画ファイル一覧を返す"""
    result = {"videos": [], "bgm": [], "se": []}
    for f in (WORKSPACE / "output").glob("*.mp4"):
        result["videos"].append(f"output/{f.name}")
    for f in (WORKSPACE / "bgm").glob("*"):
        if f.suffix in (".mp3", ".wav", ".m4a"):
            result["bgm"].append(f"bgm/{f.name}")
    for f in (WORKSPACE / "assets").glob("*"):
        if f.suffix in (".mp3", ".wav"):
            result["se"].append(f"assets/{f.name}")
    return result


@app.get("/media/{path:path}")
def serve_media(path: str):
    """動画・音声・画像ファイルを配信"""
    full = WORKSPACE / path
    if not full.exists():
        raise HTTPException(404, f"Not found: {path}")
    ext_to_mime = {
        ".mp4": "video/mp4", ".mp3": "audio/mpeg",
        ".wav": "audio/wav", ".jpg": "image/jpeg",
        ".png": "image/png",
    }
    mime = ext_to_mime.get(full.suffix, "application/octet-stream")
    return FileResponse(str(full), media_type=mime)


@app.post("/api/thumbnail_preview")
async def thumbnail_preview(request: Request):
    """指定秒数のフレームを 640×360 で返す"""
    body     = await request.json()
    seek     = float(body.get("seek_time", 10))
    src_rel  = body.get("source", "output/final_16x9.mp4")
    src      = WORKSPACE / src_rel
    out      = str(WORKSPACE / "output" / "thumbnail_preview.jpg")

    if not src.exists():
        raise HTTPException(404, "ソース動画が見つかりません")

    import subprocess
    subprocess.run(
        f'{FFMPEG} -y -ss {seek:.2f} -i "{src}" '
        f'-vf "scale=640:360:force_original_aspect_ratio=increase,crop=640:360" '
        f'-frames:v 1 "{out}"',
        shell=True, capture_output=True,
    )
    if not os.path.exists(out):
        raise HTTPException(500, "フレーム抽出失敗")
    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


# ──────────────────────────────────────────────────
# エクスポート（非同期・バックグラウンド実行）
# ──────────────────────────────────────────────────

_export: dict = {"running": False, "log": [], "done": False, "error": ""}


@app.get("/api/export/status")
def export_status():
    return _export


@app.post("/api/export/start")
async def export_start(request: Request):
    global _export
    if _export["running"]:
        return JSONResponse({"error": "エクスポート実行中です"}, status_code=409)

    state = await request.json()
    save_state(state)
    _export = {"running": True, "log": [], "done": False, "error": ""}

    threading.Thread(target=_run_export, args=(state,), daemon=True).start()
    return {"ok": True}


def _log(msg: str):
    _export["log"].append(msg)
    print(msg)


def _run_export(state: dict):
    global _export
    import sys
    sys.path.insert(0, str(WORKSPACE))

    try:
        from edit_pipeline import (
            add_bgm, add_se, add_end_card,
            generate_thumbnail, cut_silence, get_duration,
            _drawtext, _apply_vf,
        )

        src_rel = state.get("source_video", "")
        src     = WORKSPACE / src_rel
        if not src.exists():
            raise FileNotFoundError(f"ソース動画が見つかりません: {src_rel}")

        current = str(src)
        _log("🚀 エクスポート開始...")

        # ── 字幕焼き込み ──────────────────────────
        subs  = state.get("subtitles", [])
        style = state.get("style_template", {})
        if subs:
            _log("📝 字幕を焼き込み中...")
            srt_out = str(WORKSPACE / "output" / "editor_subtitles.srt")
            _write_srt(subs, srt_out)
            step = str(WORKSPACE / "output" / "e01_subtitled.mp4")
            _burn_subs(current, step, subs, style=style)
            current = step
            _log("   ✅ 字幕完了")

        # ── 無音カット ─────────────────────────────
        if state.get("cut_silence"):
            _log("✂️  無音カット中...")
            step = str(WORKSPACE / "output" / "e02_cut.mp4")
            cut_silence(current, step)
            current = step
            _log("   ✅ 無音カット完了")

        # ── BGM ───────────────────────────────────
        bgm = state.get("bgm", {})
        if bgm.get("enabled"):
            _log("🎵 BGM挿入中...")
            bgm_file = None
            if bgm.get("file"):
                candidate = WORKSPACE / bgm["file"]
                if candidate.exists():
                    bgm_file = str(candidate)
            step = str(WORKSPACE / "output" / "e03_bgm.mp4")
            add_bgm(current, step, bgm_path=bgm_file, volume=bgm.get("volume", 0.12))
            current = step
            _log("   ✅ BGM完了")

        # ── SE ────────────────────────────────────
        se = state.get("se", {})
        if se.get("enabled") and se.get("events"):
            _log("🔊 SE挿入中...")
            events = []
            se_file = se.get("file", "")
            for ev in se["events"]:
                f = WORKSPACE / se_file
                if f.exists():
                    events.append({
                        "time":   ev["time"],
                        "file":   str(f),
                        "volume": ev.get("volume", 0.7),
                    })
            if events:
                step = str(WORKSPACE / "output" / "e04_se.mp4")
                add_se(current, step, events)
                current = step
            _log(f"   ✅ SE完了 ({len(events)}件)")

        # ── エンドカード ───────────────────────────
        ec = state.get("end_card", {})
        if ec.get("enabled"):
            _log("🎬 エンドカード合成中...")
            step = str(WORKSPACE / "output" / "e05_endcard.mp4")
            add_end_card(
                current, step,
                text=ec.get("text", "チャンネル登録よろしく！"),
                subtext=ec.get("subtext", ""),
                duration=float(ec.get("duration", 5)),
            )
            current = step
            _log("   ✅ エンドカード完了")

        # ── 最終コピー ─────────────────────────────
        final = str(WORKSPACE / "output" / "final_16x9.mp4")
        shutil.copy(current, final)
        _log(f"📹 最終出力: final_16x9.mp4")

        # ── サムネイル ─────────────────────────────
        thumb = state.get("thumbnail", {})
        if thumb.get("enabled"):
            _log("🖼️  サムネイル生成中...")
            dur   = max(get_duration(final), 1)
            ratio = min(thumb.get("seek_time", 30.0) / dur, 0.95)
            generate_thumbnail(
                final,
                str(WORKSPACE / "output" / "thumbnail.jpg"),
                title=thumb.get("title", ""),
                seek_ratio=ratio,
            )
            _log("   ✅ サムネイル完了")

        _export["done"]    = True
        _log("✅ エクスポート完了！")

    except Exception as e:
        import traceback
        _export["error"] = str(e)
        _log(f"❌ エラー: {e}")
        _log(traceback.format_exc())
    finally:
        _export["running"] = False


def _write_srt(subs: list, path: str) -> None:
    def fmt(t):
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(subs, 1):
            f.write(f"{i}\n{fmt(seg['start'])} --> {fmt(seg['end'])}\n{seg['text']}\n\n")


def _burn_subs(input_path: str, output_path: str, subs: list, style: dict = None) -> None:
    import sys
    sys.path.insert(0, str(WORKSPACE))
    from edit_pipeline import (
        _drawtext, _apply_vf,
        SUB_FONTSIZE, EMP_COLOR, SUB_COLOR, SUB_BOX_COLOR, SUB_BOX_PAD,
    )

    style = style or {}

    # スタイルテンプレートから共通設定を取得
    tmpl_outline_w = int(style.get("outline_width", 0))
    tmpl_outline_c = style.get("outline_color", "#000000")
    tmpl_color     = style.get("color", "#ffffff")
    tmpl_emph_c    = style.get("emphasis_color", "yellow")

    # hex → ffmpeg カラー形式
    def to_ffcolor(c: str) -> str:
        if c.startswith("#"):
            return "0x" + c[1:]
        return c

    # 背景色名 → ffmpeg カラー文字列
    BG_COLOR_MAP = {
        "black":    "black",
        "darkgray": "0x1a1a1a",
        "gray":     "0x555555",
        "white":    "white",
    }

    vf = []
    for seg in subs:
        # ==mark== 記法をストリップしてプレーンテキストにする
        import re as _re
        text = _re.sub(r"==(.+?)==", r"\1", seg.get("text", "")).strip()
        start = seg["start"]
        end   = max(seg["end"], start + 0.3)
        lines = textwrap.wrap(text, width=28, break_long_words=True)
        disp  = "\\n".join(lines)

        # フォントサイズ・文字色（per-clip 優先 → テンプレート → デフォルト）
        size = int(seg.get("fontsize", style.get("font_size", SUB_FONTSIZE)))
        if seg.get("emphasis"):
            fontcolor = to_ffcolor(tmpl_emph_c)
        elif seg.get("fontcolor") == "red":
            fontcolor = "0xff5c5c"
        elif seg.get("fontcolor") == "cyan":
            fontcolor = "0x5ce0f5"
        else:
            fontcolor = to_ffcolor(tmpl_color)

        # テキスト背景（per-clip 優先 → テンプレート）
        bg_enabled = seg.get("bg_enabled", style.get("bg_enabled", True))
        bg_name    = seg.get("bg_color",   style.get("bg_color",   "black"))
        bg_opacity = float(seg.get("bg_opacity", style.get("bg_opacity", 0.65)))
        bg_pad     = int(seg.get("bg_padding",   style.get("bg_padding",  SUB_BOX_PAD)))

        if bg_enabled and bg_name and bg_name != "none":
            ffmpeg_color = BG_COLOR_MAP.get(bg_name, "black")
            box_color = f"{ffmpeg_color}@{bg_opacity:.2f}"
        else:
            box_color = ""

        vf.append(_drawtext(
            disp, start, end,
            fontsize=size,
            fontcolor=fontcolor,
            box_color=box_color,
            box_pad=bg_pad,
            border_width=tmpl_outline_w,
            border_color=tmpl_outline_c,
        ))
    _apply_vf(input_path, output_path, vf)


# ──────────────────────────────────────────────────
# 静的ファイル配信
# ──────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(str(WORKSPACE / "editor" / "index.html"), media_type="text/html")


@app.get("/{path:path}")
def static_catch(path: str):
    full = WORKSPACE / "editor" / path
    if full.exists() and full.is_file():
        ext_map = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        return FileResponse(str(full), media_type=ext_map.get(full.suffix, "text/plain"))
    raise HTTPException(404)


# ──────────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser, time
    print("🎬 動画エディタ起動中...")
    print("   → http://localhost:8080")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8080")).start()
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
