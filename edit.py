#!/usr/bin/env python3
"""
edit.py — 動画をフォルダに入れて名前を言うだけで全自動編集 → エディタ起動

使い方:
  python edit.py                      # input/ の動画を一覧表示して選択
  python edit.py テスト               # "テスト" を含む動画を自動選択
  python edit.py "テスト動画.mp4"     # ファイル名を直接指定
  python edit.py テスト --no-editor   # 編集のみ（エディタを開かない）
  python edit.py テスト --skip-pipeline  # パイプラインをスキップしてエディタだけ開く
"""

import os
import re
import sys
import time
import json
import shutil
import signal
import subprocess
import webbrowser
import threading
from pathlib import Path
from difflib import SequenceMatcher

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WORKSPACE   = Path(__file__).parent
INPUT_DIR   = WORKSPACE / "input"
OUTPUT_DIR  = WORKSPACE / "output"
STATE_FILE  = OUTPUT_DIR / "edit_state.json"
EDITOR_PORT = 8080
VIDEO_EXTS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

# venv の Python を優先使用（なければ sys.executable）
def _find_python() -> str:
    for cand in [
        WORKSPACE / ".venv" / "bin" / "python3",
        WORKSPACE / ".venv" / "bin" / "python",
        WORKSPACE / "venv"  / "bin" / "python3",
    ]:
        if cand.exists():
            return str(cand)
    return sys.executable

PYTHON = _find_python()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ターミナル色
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _c(code, text): return f"\033[{code}m{text}\033[0m"
def bold(t):   return _c("1", t)
def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def blue(t):   return _c("34", t)
def cyan(t):   return _c("36", t)
def dim(t):    return _c("2", t)
def red(t):    return _c("31", t)

def header(msg):
    w = 56
    print()
    print(cyan("━" * w))
    print(f"  {bold(msg)}")
    print(cyan("━" * w))

def step(emoji, msg):
    print(f"\n{emoji}  {bold(msg)}")

def ok(msg):
    print(f"   {green('✔')}  {msg}")

def warn(msg):
    print(f"   {yellow('⚠')}  {msg}")

def err(msg):
    print(f"   {red('✘')}  {msg}")
    sys.exit(1)

def info(msg):
    print(f"   {dim(msg)}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 動画ファイルの検索
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def find_videos() -> list[Path]:
    INPUT_DIR.mkdir(exist_ok=True)
    return sorted(
        [f for f in INPUT_DIR.iterdir() if f.suffix.lower() in VIDEO_EXTS],
        key=lambda f: f.stat().st_mtime,
        reverse=True,  # 新しいものが先
    )

def pick_video(query: str) -> Path:
    """クエリ文字列にマッチする動画をinput/から選ぶ"""
    videos = find_videos()

    if not videos:
        err(
            f"input/ フォルダに動画が見つかりません。\n"
            f"   {INPUT_DIR} に動画ファイルを入れてください。"
        )

    if not query:
        # クエリなし → 1本なら自動選択、複数なら一覧
        if len(videos) == 1:
            print(f"   → {bold(videos[0].name)} を使用します")
            return videos[0]
        print(f"\n{bold('📂 input/ フォルダの動画一覧:')}")
        for i, v in enumerate(videos, 1):
            mb = v.stat().st_size / 1024 / 1024
            age = _file_age(v)
            print(f"  {cyan(f'[{i}]')} {v.name}  {dim(f'{mb:.1f} MB  {age}')}")
        try:
            idx = int(input(f"\n番号を選択 [1-{len(videos)}]: ").strip()) - 1
            return videos[max(0, min(idx, len(videos) - 1))]
        except (ValueError, KeyboardInterrupt):
            print(); return videos[0]

    # ─ クエリあり ─
    # 1. 完全一致（ファイル名 or stem）
    for v in videos:
        if query == v.name or query == v.stem:
            return v

    # 2. 部分一致（大文字小文字無視）
    q = query.lower()
    for v in videos:
        if q in v.name.lower() or q in v.stem.lower():
            return v

    # 3. スコアベースのあいまい一致
    scored = sorted(videos, key=lambda v: _similarity(query, v.stem), reverse=True)
    best = scored[0]
    score = _similarity(query, best.stem)
    if score >= 0.4:
        warn(f"「{query}」の完全一致がないため「{best.name}」を使用します (類似度 {score:.0%})")
        return best

    # 4. 見つからなければ一覧表示
    warn(f"「{query}」に一致する動画が見つかりませんでした。")
    print(f"\n{bold('📂 input/ フォルダの動画一覧:')}")
    for i, v in enumerate(videos, 1):
        print(f"  {cyan(f'[{i}]')} {v.name}")
    try:
        idx = int(input(f"\n番号を選択 [1-{len(videos)}]: ").strip()) - 1
        return videos[max(0, min(idx, len(videos) - 1))]
    except (ValueError, KeyboardInterrupt):
        print(); return videos[0]

def _file_age(p: Path) -> str:
    sec = time.time() - p.stat().st_mtime
    if sec < 60:    return "たった今"
    if sec < 3600:  return f"{int(sec/60)}分前"
    if sec < 86400: return f"{int(sec/3600)}時間前"
    return f"{int(sec/86400)}日前"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# クリーンアップ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def clean_previous(video_path: Path):
    """前回の出力ファイルと状態ファイルをリセット"""
    OUTPUT_DIR.mkdir(exist_ok=True)

    # edit_state.json を削除してエディタに新鮮な状態で読ませる
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        info("前回の編集状態をリセットしました")

    # 番号付き中間ファイルを削除
    patterns = ["01_generated*", "02_subtitled*", "03_cut*",
                "04_bgm*", "04b_se*", "04c_endcard*",
                "editor_source.mp4", "e0*",
                "final_16x9.mp4", "thumbnail.jpg",
                "subs*.srt"]
    removed = 0
    for pat in patterns:
        for f in OUTPUT_DIR.glob(pat):
            f.unlink()
            removed += 1
    if removed:
        info(f"中間ファイル {removed} 件を削除しました")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# パイプライン実行
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# フィルタ: run.py の出力から人間向けの行だけ表示する
_PIPELINE_SHOW = re.compile(
    r"(Phase|✅|⏭|📝|✂|🎵|🔊|🎬|📹|🖼|❌|Error|WARNING|Traceback|"
    r"字幕|無音|BGM|SE|エンド|サムネ|完了|開始|処理|検出|生成|抽出|合成|フレーム|書き出)",
    re.IGNORECASE,
)

def run_pipeline(video_path: Path) -> bool:
    """run.py を --input --no-interactive モードで実行する"""
    step("⚙️ ", f"自動編集パイプライン開始")
    info(f"入力動画: {video_path.name}")
    info("字幕 → 無音カット → BGM → エンドカード → サムネイル")
    print()

    cmd = [
        PYTHON, str(WORKSPACE / "run.py"),
        "--input", str(video_path),
        "--no-interactive",
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(WORKSPACE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # リアルタイムで出力を表示
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        # 重要な行だけ表示
        if _PIPELINE_SHOW.search(line):
            if "❌" in line or "Error" in line.lower():
                print(f"   {red(line)}")
            elif "✅" in line or "完了" in line:
                print(f"   {green(line)}")
            elif "Phase" in line:
                print(f"\n   {blue(line)}")
            else:
                print(f"   {line}")

    proc.wait()

    if proc.returncode != 0:
        err(f"パイプラインがエラーで終了しました (code={proc.returncode})")
        return False

    # 出力ファイルの確認
    print()
    final = OUTPUT_DIR / "final_16x9.mp4"
    if final.exists():
        mb = final.stat().st_size / 1024 / 1024
        ok(f"最終動画: {green('output/final_16x9.mp4')}  ({mb:.1f} MB)")
    else:
        warn("final_16x9.mp4 が見つかりません")

    thumb = OUTPUT_DIR / "thumbnail.jpg"
    if thumb.exists():
        ok(f"サムネイル: {green('output/thumbnail.jpg')}")

    return True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# エディタ起動
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def stop_existing_editor():
    """ポート 8080 を使用中のプロセスを終了する"""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{EDITOR_PORT}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        if pids:
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            time.sleep(0.8)
            info(f"既存のエディタ (PID {', '.join(pids)}) を終了しました")
    except Exception:
        pass

def launch_editor():
    """editor.py を起動してブラウザを開く"""
    step("🌐", "編集エディタを起動中...")

    stop_existing_editor()

    proc = subprocess.Popen(
        [PYTHON, str(WORKSPACE / "editor.py")],
        cwd=str(WORKSPACE),
        # editor.py は自分でブラウザを開くので stdout はそのまま
    )

    # uvicorn の起動を待つ（最大10秒）
    import urllib.request
    for _ in range(20):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://localhost:{EDITOR_PORT}/", timeout=1)
            break  # 応答あり → 起動完了
        except Exception:
            pass
    else:
        if proc.poll() is not None:
            err("エディタの起動に失敗しました")

    ok(f"エディタ起動完了: {cyan(f'http://localhost:{EDITOR_PORT}')}")
    print()
    print(dim("  エディタを終了するには Ctrl+C を押してください"))
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print(f"\n\n{yellow('エディタを終了します...')}")
        proc.terminate()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_args():
    args     = sys.argv[1:]
    no_edit  = "--no-editor"      in args
    skip_pl  = "--skip-pipeline"  in args
    query_parts = [a for a in args if not a.startswith("--")]
    query    = " ".join(query_parts).strip()
    return query, no_edit, skip_pl

def main():
    query, no_editor, skip_pipeline = parse_args()

    header("🎬 動画自動編集 & エディタ起動")

    # ─ 動画を選択 ─────────────────────────────────
    step("📂", "動画を検索中...")
    video = pick_video(query)
    print()
    ok(f"選択: {bold(video.name)}  {dim(f'({video.stat().st_size/1024/1024:.1f} MB)')}")

    # ─ クリーンアップ ─────────────────────────────
    step("🗑 ", "前回の出力をリセット中...")
    clean_previous(video)

    if not skip_pipeline:
        # ─ パイプライン実行 ──────────────────────────
        header("⚙️  自動編集パイプライン")
        success = run_pipeline(video)
        if not success:
            sys.exit(1)
        ok(bold("全パイプライン完了！"))
    else:
        warn("--skip-pipeline が指定されたため編集をスキップします")

    if not no_editor:
        # ─ エディタ起動 ───────────────────────────────
        header("✏️  編集エディタ")
        launch_editor()
    else:
        info("--no-editor が指定されたためエディタを開きません")
        print(f"\n   エディタを開くには: {cyan('python editor.py')}")

if __name__ == "__main__":
    main()
