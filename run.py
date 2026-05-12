"""
run.py — YouTube 16:9 動画編集 メインスクリプト
─────────────────────────────────────────────────
使い方:
  python run.py                              # インタラクティブ設定
  python run.py --prompt "a mountain scene" # プロンプト直接指定
  python run.py --input my_video.mp4        # 既存動画を編集のみ
  python run.py --input my_video.mp4 --no-interactive  # 設定ファイルで全自動

出力: output/final_16x9.mp4 + output/thumbnail.jpg（16:9 / YouTube用）
      output/final_vertical.mp4（--vertical 指定時のみ・ショート用）
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = "output/session_config.json"

DEFAULT_CONFIG = {
    # ── 動画生成 ──────────────────────────────
    "prompt":            "",
    "aspect_ratio":      "16:9",       # YouTube 16:9 固定
    "duration":          5,            # 5 or 10 秒
    "language":          "ja",         # Whisper 音声認識言語

    # ── 編集ステップ（True/False で個別 ON/OFF）─
    "add_subtitles":     True,         # 字幕焼き込み（強調・アニメ込み）
    "cut_silence":       True,         # 無音カット
    "add_bgm":           True,         # BGM挿入
    "bgm_path":          None,         # None でフリーBGM自動DL
    "bgm_volume":        0.12,         # BGM音量（0.0–1.0）

    # ── SE（効果音）────────────────────────────
    "add_se":            False,        # SE挿入（半自動）
    "se_file":           "",           # SE ファイルパス（例: assets/whoosh.wav）
    "se_volume":         0.7,          # SE 音量
    # se_events は自動でカット割り検出結果から生成

    # ── エンドカード ────────────────────────────
    "add_end_card":      True,         # エンドカード合成
    "end_card_text":     "チャンネル登録よろしく！",
    "end_card_subtext":  "↑ 高評価もお願いします",
    "end_card_sec":      5,            # エンドカード表示秒数

    # ── サムネイル ──────────────────────────────
    "generate_thumbnail": True,
    "thumbnail_title":   "",           # サムネイル上のタイトル文字

    # ── ショート用縦型変換（デフォルト無効）─────
    "convert_vertical":  False,
    "vertical_title":    "",
}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        cfg = {**DEFAULT_CONFIG, **saved}
        print(f"📂 前回の設定を読み込みました: {CONFIG_FILE}")
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    os.makedirs("output", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def ask(question: str, default: str = "") -> str:
    hint   = f" [{default}]" if default else ""
    answer = input(f"{question}{hint}: ").strip()
    return answer if answer else default


# =============================================
# インタラクティブ設定
# =============================================

def interactive_setup(cfg: dict, skip_prompt: bool = False) -> dict:
    print("\n" + "=" * 52)
    print("🎬 YouTube 16:9 動画編集 — 設定")
    print("=" * 52)

    if not skip_prompt:
        cfg["prompt"] = ask(
            "① 動画の内容プロンプト（英語推奨）\n   例: a calm forest with sunlight",
            cfg.get("prompt", "")
        )

    dur = ask("② 動画の長さ  [5]=5秒  [10]=10秒", "5")
    cfg["duration"] = 10 if dur == "10" else 5

    subs = ask("③ 字幕（強調テロップ・アニメ込み）？  [y/n]", "y")
    cfg["add_subtitles"] = subs.lower() != "n"

    silence = ask("④ 無音カット？  [y/n]", "y")
    cfg["cut_silence"] = silence.lower() != "n"

    bgm = ask("⑤ BGM追加？  [y/n]", "y")
    cfg["add_bgm"] = bgm.lower() != "n"
    if cfg["add_bgm"]:
        p = ask("   BGMファイルパス（Enterで自動DL）", cfg.get("bgm_path") or "")
        cfg["bgm_path"] = p if p else None

    se = ask("⑥ SE効果音を追加？（カット割り箇所に自動挿入）  [y/n]", "n")
    cfg["add_se"] = se.lower() == "y"
    if cfg["add_se"]:
        p = ask("   SE ファイルパス（例: assets/whoosh.wav）", cfg.get("se_file", ""))
        cfg["se_file"] = p

    ec = ask("⑦ エンドカード追加？  [y/n]", "y")
    cfg["add_end_card"] = ec.lower() != "n"
    if cfg["add_end_card"]:
        t = ask("   メインテキスト", cfg.get("end_card_text", "チャンネル登録よろしく！"))
        cfg["end_card_text"] = t

    thumb = ask("⑧ サムネイル自動生成？  [y/n]", "y")
    cfg["generate_thumbnail"] = thumb.lower() != "n"
    if cfg["generate_thumbnail"]:
        t = ask("   サムネイルタイトル文字（任意）", cfg.get("thumbnail_title", "")[:30])
        cfg["thumbnail_title"] = t

    vertical = ask("⑨ ショート用縦型（9:16）も作成？  [y/n]", "n")
    cfg["convert_vertical"] = vertical.lower() == "y"
    if cfg["convert_vertical"]:
        cfg["vertical_title"] = ask("   縦型タイトル（任意）", "")

    save_config(cfg)
    print("\n✅ 設定を保存しました\n")
    return cfg


# =============================================
# メイン処理
# =============================================

def main():
    parser = argparse.ArgumentParser(description="YouTube 16:9 AI Video Editor")
    parser.add_argument("--prompt",  "-p", default="", help="動画生成プロンプト")
    parser.add_argument("--input",   "-i", default="", help="既存動画パス（生成スキップ）")
    parser.add_argument("--vertical",      action="store_true", help="縦型ショートも作成")
    parser.add_argument("--no-interactive", action="store_true", help="設定ファイルで全自動")
    args = parser.parse_args()

    for d in ("output", "input", "bgm", "assets"):
        os.makedirs(d, exist_ok=True)

    cfg = load_config()
    if args.prompt:
        cfg["prompt"] = args.prompt
    if args.vertical:
        cfg["convert_vertical"] = True

    if not args.no_interactive:
        cfg = interactive_setup(cfg, skip_prompt=bool(args.prompt))

    if not cfg.get("prompt") and not args.input:
        print("❌ プロンプトが未設定です。--prompt か --input を指定してください。")
        sys.exit(1)

    print("\n" + "=" * 52)
    print("🚀 処理開始（YouTube 16:9 出力）")
    print("=" * 52)

    # ──────────────────────────────────────────
    # Phase 1: 動画生成（Kling AI）or 既存動画
    # ──────────────────────────────────────────
    if args.input:
        current = args.input
        print(f"\n📂 既存動画を使用: {current}")
    else:
        from generate import generate_from_text, check_credits
        check_credits()
        current = generate_from_text(
            prompt=cfg["prompt"],
            duration=cfg["duration"],
            aspect_ratio="16:9",    # YouTube 16:9 固定
            output_path="output/01_generated.mp4",
        )

    # ──────────────────────────────────────────
    # エディタ用ソース保存（字幕焼き込み前）
    # エディタはこのファイルをプレビューに使い、
    # JS オーバーレイで字幕を表示する（二重防止）
    # ──────────────────────────────────────────
    editor_source = "output/editor_source.mp4"
    shutil.copy(current, editor_source)
    print(f"📌 エディタ用ソース保存: {editor_source}")

    # ──────────────────────────────────────────
    # Phase 2: 字幕（フルテロップ + 強調 + アニメ）
    # ──────────────────────────────────────────
    srt_path = "output/02_subtitled.srt"   # カット割り検出に後で使う
    if cfg["add_subtitles"]:
        from edit_pipeline import add_subtitles
        current = add_subtitles(
            current, "output/02_subtitled.mp4",
            language=cfg["language"],
        )
        # SRT は add_subtitles が output_path と同名で保存している

    # ──────────────────────────────────────────
    # Phase 2b: カット割り検出（SE挿入に使用）
    # ──────────────────────────────────────────
    topic_cuts = []
    if os.path.exists(srt_path):
        from edit_pipeline import detect_topic_cuts
        topic_cuts = detect_topic_cuts(srt_path, gap_threshold=1.5)

    # ──────────────────────────────────────────
    # Phase 3: 無音カット
    # ──────────────────────────────────────────
    if cfg["cut_silence"]:
        from edit_pipeline import cut_silence
        current = cut_silence(current, "output/03_cut.mp4")

    # ──────────────────────────────────────────
    # Phase 4: BGM
    # ──────────────────────────────────────────
    if cfg["add_bgm"]:
        from edit_pipeline import add_bgm
        current = add_bgm(
            current, "output/04_bgm.mp4",
            bgm_path=cfg.get("bgm_path"),
            volume=cfg.get("bgm_volume", 0.12),
        )

    # ──────────────────────────────────────────
    # Phase 4b: SE（効果音）挿入（半自動）
    # ──────────────────────────────────────────
    if cfg["add_se"] and cfg.get("se_file"):
        from edit_pipeline import add_se
        # カット割り検出結果をSEタイミングとして使用
        if topic_cuts:
            se_events = [
                {"time": c["time"], "file": cfg["se_file"], "volume": cfg.get("se_volume", 0.7)}
                for c in topic_cuts
            ]
        else:
            # カット割りなし → 動画の中間地点に1回
            dur = 0.0
            try:
                from edit_pipeline import get_duration
                dur = get_duration(current)
            except Exception:
                pass
            se_events = [{"time": dur / 2, "file": cfg["se_file"], "volume": cfg.get("se_volume", 0.7)}] if dur > 2 else []
        current = add_se(current, "output/04b_se.mp4", se_events)

    # ──────────────────────────────────────────
    # Phase 4c: エンドカード
    # ──────────────────────────────────────────
    if cfg["add_end_card"]:
        from edit_pipeline import add_end_card
        current = add_end_card(
            current, "output/04c_endcard.mp4",
            text=cfg.get("end_card_text", "チャンネル登録よろしく！"),
            subtext=cfg.get("end_card_subtext", "↑ 高評価もお願いします"),
            duration=cfg.get("end_card_sec", 5),
        )

    # ──────────────────────────────────────────
    # Phase 5: 最終出力（16:9）
    # ──────────────────────────────────────────
    final_16x9 = "output/final_16x9.mp4"
    shutil.copy(current, final_16x9)

    # ──────────────────────────────────────────
    # Phase 5b: サムネイル
    # ──────────────────────────────────────────
    thumbnail = None
    if cfg["generate_thumbnail"]:
        from edit_pipeline import generate_thumbnail
        thumbnail = generate_thumbnail(
            final_16x9, "output/thumbnail.jpg",
            title=cfg.get("thumbnail_title", ""),
        )

    # ──────────────────────────────────────────
    # Phase 6: 縦型変換（オプション・ショート用）
    # ──────────────────────────────────────────
    final_vertical = None
    if cfg["convert_vertical"]:
        from edit_pipeline import convert_to_vertical
        final_vertical = convert_to_vertical(
            final_16x9, "output/final_vertical.mp4",
            title=cfg.get("vertical_title", ""),
        )

    # ──────────────────────────────────────────
    # 完了レポート
    # ──────────────────────────────────────────
    def file_info(p):
        if p and os.path.exists(p):
            mb = os.path.getsize(p) / 1024 / 1024
            return f"{p} ({mb:.1f} MB)"
        return "スキップ"

    print("\n" + "=" * 52)
    print("✅ 完了！")
    print("=" * 52)
    print(f"📹 16:9 動画    : {file_info(final_16x9)}")
    if thumbnail:
        print(f"🖼️  サムネイル  : {file_info(thumbnail)}")
    if final_vertical:
        print(f"📱 縦型動画     : {file_info(final_vertical)}")
    if topic_cuts:
        print(f"🔀 カット割り   : {len(topic_cuts)}箇所 検出")
    print(f"\n📂 出力先: {os.path.abspath('output/')}")
    print("=" * 52)
    print("\n編集要素ステータス:")
    flags = {
        "フルテロップ（字幕）":     cfg["add_subtitles"],
        "強調テロップ（自動検出）":  cfg["add_subtitles"],
        "テロップアニメ":            cfg["add_subtitles"],
        "無音カット":                cfg["cut_silence"],
        "BGM挿入":                   cfg["add_bgm"],
        "SE効果音":                  cfg.get("add_se", False),
        "エンドカード":              cfg["add_end_card"],
        "サムネイル":                cfg["generate_thumbnail"],
        "縦型変換（ショート）":      cfg["convert_vertical"],
    }
    for k, v in flags.items():
        print(f"  {'✅' if v else '⏭️ '} {k}")

    print("\n" + "=" * 52)
    print("✏️  手動編集エディタを起動するには:")
    print("   python editor.py")
    print("   → http://localhost:8080 をブラウザで開く")
    print("=" * 52)

if __name__ == "__main__":
    main()
