"""
edit_pipeline.py — YouTube 16:9 動画編集パイプライン
─────────────────────────────────────────────────────
対応機能:
  ✅ フルテロップ（字幕）   Whisper + ffmpeg drawtext
  ✅ テロップデザイン統一   フォント・色・サイズを定数で管理
  ✅ テロップアニメーション  フェードイン/アウト（drawtext alpha式）
  🟡 強調テロップ           数字・感嘆符・短断言フレーズを黄色/大きく自動検出
  ✅ 無音カット              ffmpeg silencedetect → select フィルタ
  🟡 カット割り検出          Whisper セグメント間ギャップ解析
  ✅ BGM挿入                フェードイン/アウト付きで元音声にミックス
  🟡 SE（効果音）           タイムスタンプ指定で adelay ミックス
  ✅ エンドカード            末尾N秒に半透明オーバーレイ + テキスト描画
  ✅ サムネイル生成          1280×720 フレーム抽出 + Pillow タイトル合成
  ✅ 縦型変換（オプション）  9:16 / 1080×1920（ショート用、デフォルト無効）

出力仕様: YouTube 16:9 (1920×1080)
"""

import os
import re
import json
import subprocess
import textwrap
import tempfile
from pathlib import Path

# ffmpeg-full（libfreetype入り）を優先使用
_FF_FULL  = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
_FP_FULL  = "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
FFMPEG    = _FF_FULL if os.path.exists(_FF_FULL) else "ffmpeg"
FFPROBE   = _FP_FULL if os.path.exists(_FP_FULL) else "ffprobe"

# =============================================
# 出力仕様（YouTube 16:9）
# =============================================
OUTPUT_W = 1920
OUTPUT_H = 1080

# ─── 字幕デザイン（統一設定）─────────────────
SUB_FONTSIZE   = 52          # 通常テロップ フォントサイズ (px)
SUB_COLOR      = "white"     # 文字色
SUB_BOX_COLOR  = "black@0.65"  # 背景ボックス色（透明度付き）
SUB_BOX_PAD    = 10          # 背景ボックス パディング (px)
SUB_SHADOW     = 2           # 影オフセット (px)
SUB_Y_MARGIN   = 80          # 画面下端からの余白 (px)
SUB_MAX_CHARS  = 28          # 1行最大文字数（折り返し）
SUB_FADE       = 0.15        # フェードイン/アウト秒数

# ─── 強調テロップ設定（サイズは通常と同一・色のみ変更）──
EMP_FONTSIZE   = SUB_FONTSIZE  # サイズ統一（色だけ変える）
EMP_COLOR      = "yellow"      # 強調文字色
EMP_BOX_COLOR  = "black@0.80"  # 強調背景

# ─── エンドカード設定 ────────────────────────
END_CARD_SEC   = 5           # エンドカード表示秒数
END_CARD_BG    = "black@0.55"  # 背景オーバーレイ色

# =============================================
# ユーティリティ
# =============================================

def run(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """シェルコマンド実行"""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"コマンド失敗:\n  CMD: {cmd}\n  ERR: {result.stderr[-600:]}")
    return result


def get_duration(video_path: str) -> float:
    """動画の長さ（秒）を返す"""
    r = run(f'{FFPROBE} -v quiet -print_format json -show_streams "{video_path}"')
    for s in json.loads(r.stdout)["streams"]:
        if s.get("codec_type") == "video":
            return float(s.get("duration", 0))
    return 0.0


def has_audio(video_path: str) -> bool:
    """音声トラックの有無を確認"""
    r = run(f'{FFPROBE} -v quiet -print_format json -show_streams "{video_path}"')
    return any(s.get("codec_type") == "audio" for s in json.loads(r.stdout)["streams"])


def _find_japanese_font() -> str:
    """システムから日本語フォントファイルを検索"""
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
        "/Library/Fonts/Arial Unicode MS.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJKjp-Regular.otf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # fc-list フォールバック
    r = run("fc-list :lang=ja --format='%{file}\\n' 2>/dev/null | head -1", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0].strip()
    return ""


JAPANESE_FONT = _find_japanese_font()


def _esc(text: str) -> str:
    """drawtext フィルタ用の特殊文字エスケープ"""
    return (
        text
        .replace("\\", "\\\\")
        .replace("'",  "\u2019")   # ' → '（代替文字）
        .replace(":",  "\\:")
        .replace("%",  "\\%")
        .replace("[",  "\\[")
        .replace("]",  "\\]")
        .replace(",",  "\\,")
    )


def _font_arg() -> str:
    """drawtext 用フォントファイル引数を生成"""
    if JAPANESE_FONT:
        safe = JAPANESE_FONT.replace("'", "\\'").replace(":", "\\:")
        return f":fontfile='{safe}'"
    return ""


def _drawtext(text: str, start: float, end: float,
              fontsize: int = SUB_FONTSIZE,
              fontcolor: str = SUB_COLOR,
              box_color: str = SUB_BOX_COLOR,
              box_pad: int = SUB_BOX_PAD,
              border_width: int = 0,
              border_color: str = "black",
              fade: bool = True) -> str:
    """
    単一セグメントの drawtext フィルタ式を生成
    - フェードイン/アウト（alpha 式）
    - 背景ボックス（box_color="" で無効化）
    - box_pad: 背景の余白 px
    - border_width/border_color: テキストアウトライン
    """
    safe = _esc(text)
    fa   = _font_arg()
    fd   = SUB_FADE

    if fade and (end - start) > fd * 2.5:
        alpha_expr = (
            f"if(lt(t-{start:.3f},{fd}),(t-{start:.3f})/{fd},"
            f"if(gt(t,{end:.3f}-{fd}),({end:.3f}-t)/{fd},1))"
        )
        alpha_arg = f":alpha='{alpha_expr}'"
    else:
        alpha_arg = ""

    # 背景ボックス（box_color が空の場合は非表示）
    if box_color:
        box_str = f":box=1:boxcolor={box_color}:boxborderw={box_pad}"
    else:
        box_str = ":box=0"

    # テキストアウトライン（border_width > 0 のとき）
    if border_width > 0:
        # hex (#000000) → FFmpeg形式 (0x000000)
        bc = border_color.replace("#", "0x") if border_color.startswith("#") else border_color
        border_str = f":borderw={border_width}:bordercolor={bc}"
        shadow_str = ""  # アウトラインがあれば影は不要
    else:
        border_str = ""
        shadow_str = f":shadowcolor=black:shadowx={SUB_SHADOW}:shadowy={SUB_SHADOW}"

    return (
        f"drawtext=text='{safe}'"
        f"{fa}"
        f":fontsize={fontsize}:fontcolor={fontcolor}"
        f":x=(w-text_w)/2:y=h-th-{SUB_Y_MARGIN}"
        f"{box_str}"
        f"{border_str}"
        f"{shadow_str}"
        f":enable='between(t,{start:.3f},{end:.3f})'"
        f"{alpha_arg}"
    )


def _apply_vf(input_path: str, output_path: str, vf_parts: list,
              extra_flags: str = "") -> None:
    """
    ビデオフィルタを適用して出力（音声はコピー）
    フィルタ文字列が長い場合は filter_complex_script 経由で渡す
    """
    parts = [p for p in vf_parts if p and p != "null"]
    if not parts:
        run(f'{FFMPEG} -y -i "{input_path}" -c copy {extra_flags} "{output_path}"')
        return

    vf = ",".join(parts)

    if len(vf) <= 60_000:
        # 通常の -vf フラグ
        run(f'{FFMPEG} -y -i "{input_path}" -vf "{vf}" -c:a copy {extra_flags} "{output_path}"')
    else:
        # フィルタが長すぎる場合: filter_complex_script で渡す
        fc_str = "[0:v]" + vf + "[vout]"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            f.write(fc_str)
            script = f.name
        try:
            run(
                f'{FFMPEG} -y -i "{input_path}" '
                f'-filter_complex_script "{script}" '
                f'-map "[vout]" -map 0:a? -c:a copy {extra_flags} "{output_path}"'
            )
        finally:
            os.unlink(script)


# =============================================
# Step 1: 字幕（フルテロップ + 強調 + アニメーション）
# =============================================

def add_subtitles(input_path: str, output_path: str,
                  language: str = "ja",
                  add_emphasis: bool = True) -> str:
    """
    Whisper で音声認識 → 字幕を動画に焼き込む
    ✅ フルテロップ（全文字幕）
    ✅ テロップデザイン統一（SUB_* 定数で管理）
    ✅ テロップアニメーション（フェードイン/アウト）
    🟡 強調テロップ（数字・感嘆符・短断言フレーズを自動検出→黄色/大きく）
    音声なし動画はスキップ
    """
    print("\n📝 [字幕] 処理開始...")

    if not has_audio(input_path):
        print("   ⚠️  音声なし → 字幕スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    try:
        import whisper
    except ImportError:
        print("   ⚠️  Whisper未インストール → スキップ（pip install openai-whisper）")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    # 音声認識
    print("   🎙️  Whisper 認識中（small モデル）...")
    model = whisper.load_model("small")
    result = model.transcribe(input_path, language=language, word_timestamps=True)
    segments = result.get("segments", [])
    words    = _flatten_words(segments)

    if not segments:
        print("   ⚠️  認識テキストなし → スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    # SRT 保存（カット割り検出などの後工程で使用）
    srt_path = str(Path(output_path).with_suffix(".srt"))
    _write_srt(segments, srt_path)
    print(f"   📄 SRT: {srt_path}  ({len(segments)} セグメント)")

    # 強調セグメント検出
    emphasis_times: set[tuple] = set()
    if add_emphasis:
        for s, e, _ in _detect_emphasis(segments):
            emphasis_times.add((round(s, 3), round(e, 3)))
        print(f"   ⭐ 強調テロップ候補: {len(emphasis_times)} 件")

    # drawtext フィルタ式を構築
    vf_parts: list[str] = []
    n_normal = 0
    n_emp    = 0

    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        start = seg["start"]
        end   = max(seg["end"], start + 0.3)

        # 折り返し
        lines = textwrap.wrap(text, width=SUB_MAX_CHARS, break_long_words=True)
        display = "\\n".join(lines)

        if (round(start, 3), round(end, 3)) in emphasis_times:
            vf_parts.append(_drawtext(
                display, start, end,
                fontsize=EMP_FONTSIZE,
                fontcolor=EMP_COLOR,
                box_color=EMP_BOX_COLOR,
            ))
            n_emp += 1
        else:
            vf_parts.append(_drawtext(display, start, end))
            n_normal += 1

    print(f"   📊 通常: {n_normal}  強調: {n_emp}")
    _apply_vf(input_path, output_path, vf_parts)
    print(f"   ✅ 字幕焼き込み完了: {output_path}")
    return output_path


def _flatten_words(segments: list) -> list:
    """Whisper セグメントから単語リストを平坦化"""
    words = []
    for seg in segments:
        for w in seg.get("words", []):
            words.append({
                "word":  w["word"].strip(),
                "start": w["start"],
                "end":   w["end"],
            })
    return words


def _detect_emphasis(segments: list) -> list[tuple]:
    """
    強調テロップ候補を検出 → [(start, end, text), ...]
    ヒューリスティック:
      - 数字（金額・統計・ランキング等）を含む
      - 感嘆符・疑問符を含む
      - 短いフレーズ（≤8文字）かつ前後に間がある（断言的な発言）
      - セグメント前の無音が 0.8 秒以上（話題の冒頭）
    """
    result = []
    for i, seg in enumerate(segments):
        text  = seg["text"].strip()
        start = seg["start"]
        end   = seg["end"]
        flag  = False

        if re.search(r'[\d０-９]', text):             # 数字
            flag = True
        if re.search(r'[！？!?]', text):              # 感嘆・疑問
            flag = True
        if len(text) <= 8 and (end - start) < 1.5:   # 短断言
            flag = True
        if i > 0:
            gap = start - segments[i - 1]["end"]
            if gap > 0.8 and len(text) <= 16:         # 話題冒頭
                flag = True

        if flag:
            result.append((start, end, text))
    return result


def _write_srt(segments: list, srt_path: str) -> None:
    """Whisper セグメントを SRT ファイルに書き出す"""
    def fmt(t: float) -> str:
        h, rem = divmod(int(t), 3600)
        m, s = divmod(rem, 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02}:{m:02}:{s:02},{ms:03}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            text  = seg["text"].strip()
            start = seg["start"]
            end   = max(seg["end"], start + 0.3)
            f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")


# =============================================
# Step 2: 無音カット
# =============================================

def cut_silence(input_path: str, output_path: str,
                noise_db: float = -35.0,
                min_silence: float = 0.5) -> str:
    """
    無音区間を検出して除去（✅ 完全自動）
    カット率 50% 超の場合は閾値を -25dB に緩和して再試行
    """
    print("\n✂️  [無音カット] 処理開始...")

    if not has_audio(input_path):
        print("   ⚠️  音声なし → スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    silence_segs = _detect_silence(input_path, noise_db, min_silence)
    orig_dur = get_duration(input_path)

    if not silence_segs:
        print("   ℹ️  無音区間なし → スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    speech_segs = _invert_silence(silence_segs, orig_dur)
    cut_ratio = 1 - sum(e - s for s, e in speech_segs) / orig_dur

    if cut_ratio > 0.5:
        print(f"   ⚠️  カット率 {cut_ratio:.0%} > 50% → 閾値を -25dB に緩和")
        silence_segs = _detect_silence(input_path, -25.0, min_silence)
        speech_segs = _invert_silence(silence_segs, orig_dur)
        cut_ratio = 1 - sum(e - s for s, e in speech_segs) / orig_dur

    select_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in speech_segs)
    run(
        f'{FFMPEG} -y -i "{input_path}" '
        f'-vf "select=\'{select_expr}\',setpts=N/FRAME_RATE/TB" '
        f'-af "aselect=\'{select_expr}\',asetpts=N/SR/TB" '
        f'"{output_path}"'
    )

    removed = orig_dur - get_duration(output_path)
    print(f"   ✅ {removed:.1f}秒除去 (カット率 {cut_ratio:.0%}): {output_path}")
    return output_path


def _detect_silence(video_path: str, noise_db: float, min_dur: float) -> list:
    r = run(
        f'{FFMPEG} -i "{video_path}" '
        f'-af "silencedetect=noise={noise_db}dB:d={min_dur}" -f null - 2>&1',
        check=False,
    )
    out = r.stdout + r.stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends   = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    return list(zip(starts, ends))


def _invert_silence(silence: list, total: float) -> list:
    speech, prev = [], 0.0
    for s, e in silence:
        if s > prev + 0.05:
            speech.append((prev, s))
        prev = e
    if prev < total - 0.05:
        speech.append((prev, total))
    return speech


# =============================================
# Step 2b: カット割り検出（話題転換）
# =============================================

def detect_topic_cuts(srt_path: str,
                      gap_threshold: float = 1.5) -> list[dict]:
    """
    SRT を解析して話題転換点（大きな無音ギャップ）を検出
    Returns: [{"time": float, "before": str, "after": str, "gap": float}, ...]

    SE 挿入・チャプターマーカー作成などに活用
    """
    if not os.path.exists(srt_path):
        return []

    segs = _parse_srt(srt_path)
    cuts = []
    for i in range(1, len(segs)):
        gap = segs[i]["start"] - segs[i - 1]["end"]
        if gap >= gap_threshold:
            cuts.append({
                "time":   segs[i - 1]["end"] + gap / 2,
                "before": segs[i - 1]["text"],
                "after":  segs[i]["text"],
                "gap":    round(gap, 2),
            })
    if cuts:
        print(f"   🔀 カット割り検出: {len(cuts)} 件 "
              f"（閾値 {gap_threshold}秒、最大ギャップ {max(c['gap'] for c in cuts):.1f}秒）")
    return cuts


def _parse_srt(srt_path: str) -> list[dict]:
    """SRT ファイルをパースして [{start, end, text}, ...] を返す"""
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()

    segs = []
    for block in re.split(r"\n\n+", content.strip()):
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
        segs.append({"start": start, "end": end, "text": text})
    return segs


# =============================================
# Step 3: BGM挿入
# =============================================

def add_bgm(input_path: str, output_path: str,
            bgm_path: str = None, volume: float = 0.12) -> str:
    """
    BGM をフェードイン/アウト付きで合成（✅ 完全自動）
    bgm_path 未指定の場合はフリーBGMを自動ダウンロード
    """
    print("\n🎵 [BGM] 処理開始...")

    if bgm_path is None:
        bgm_path = _download_free_bgm()

    if not bgm_path or not os.path.exists(bgm_path):
        print("   ⚠️  BGMファイルが見つかりません → スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    duration = get_duration(input_path)
    fo_start = max(0, duration - 3)

    bgm_chain = (
        f"[1:a]volume={volume},"
        f"afade=t=in:ss=0:d=2,"
        f"afade=t=out:st={fo_start:.2f}:d=3[bgm]"
    )

    if has_audio(input_path):
        fc = f"{bgm_chain};[0:a][bgm]amix=inputs=2:duration=first[aout]"
        run(
            f'{FFMPEG} -y -i "{input_path}" -i "{bgm_path}" '
            f'-filter_complex "{fc}" '
            f'-map 0:v -map "[aout]" -c:v copy "{output_path}"'
        )
    else:
        fc = f"{bgm_chain}"
        run(
            f'{FFMPEG} -y -i "{input_path}" -i "{bgm_path}" '
            f'-filter_complex "{fc}" '
            f'-map 0:v -map "[bgm]" -c:v copy -shortest "{output_path}"'
        )

    print(f"   ✅ BGM挿入完了: {output_path}")
    return output_path


def _download_free_bgm(out: str = "bgm/auto_bgm.mp3") -> str:
    import urllib.request
    os.makedirs("bgm", exist_ok=True)
    url = ("https://files.freemusicarchive.org/storage-freemusicarchive-org/"
           "music/WFMU/Kai_Engel/Sustain/Kai_Engel_-_01_-_Sustain.mp3")
    print("   📥 フリーBGM ダウンロード中...")
    try:
        urllib.request.urlretrieve(url, out)
        print(f"   ✅ ダウンロード完了: {out}")
        return out
    except Exception as e:
        print(f"   ⚠️  ダウンロード失敗: {e}")
        return ""


# =============================================
# Step 3b: SE（効果音）挿入（半自動）
# =============================================

def add_se(input_path: str, output_path: str,
           se_events: list[dict]) -> str:
    """
    指定タイムスタンプに SE を合成（🟡 半自動）

    Args:
        se_events: [
            {"time": 秒数, "file": "assets/whoosh.wav", "volume": 0.8},
            ...
        ]

    典型的な使い方:
        cuts = detect_topic_cuts("output/02_subtitled.srt")
        events = [{"time": c["time"], "file": "assets/se_whoosh.wav"} for c in cuts]
        add_se(current, next_path, events)
    """
    print("\n🔊 [SE] 処理開始...")

    valid = [e for e in se_events if os.path.exists(e.get("file", ""))]
    if not valid:
        print(f"   ℹ️  有効なSEイベントなし（指定数: {len(se_events)}）→ スキップ")
        run(f'cp "{input_path}" "{output_path}"')
        return output_path

    inputs = f'-i "{input_path}"'
    for ev in valid:
        inputs += f' -i "{ev["file"]}"'

    fc_parts  = []
    mix_labels = []

    if has_audio(input_path):
        mix_labels.append("[0:a]")

    for i, ev in enumerate(valid, 1):
        vol      = ev.get("volume", 0.7)
        delay_ms = int(ev["time"] * 1000)
        label    = f"[se{i}]"
        fc_parts.append(
            f"[{i}:a]volume={vol},adelay={delay_ms}|{delay_ms}{label}"
        )
        mix_labels.append(label)

    n = len(mix_labels)
    fc_parts.append(f"{''.join(mix_labels)}amix=inputs={n}:duration=first[aout]")
    fc = ";".join(fc_parts)

    run(
        f'{FFMPEG} -y {inputs} '
        f'-filter_complex "{fc}" '
        f'-map 0:v -map "[aout]" -c:v copy "{output_path}"'
    )

    print(f"   ✅ SE挿入: {len(valid)}件 → {output_path}")
    return output_path


# =============================================
# Step 4: エンドカード（末尾のチャンネル登録促進）
# =============================================

def add_end_card(input_path: str, output_path: str,
                 text: str = "チャンネル登録よろしく！",
                 subtext: str = "↑ 高評価もお願いします",
                 duration: float = END_CARD_SEC) -> str:
    """
    動画末尾 N 秒にエンドカードを合成（✅ 自動化可能）
    - 半透明の暗いオーバーレイ（下部40%）
    - メインテキスト + サブテキスト
    """
    print("\n🎬 [エンドカード] 処理開始...")

    video_dur = get_duration(input_path)
    ec_start  = max(0, video_dur - duration)
    fa        = _font_arg()
    enable    = f"gte(t,{ec_start:.3f})"

    vf_parts: list[str] = []

    # 半透明の暗い背景（下部 40%）
    vf_parts.append(
        f"drawbox=x=0:y=ih*0.60:w=iw:h=ih*0.40"
        f":color={END_CARD_BG}:t=fill:enable='{enable}'"
    )

    # メインテキスト
    safe_main = _esc(text)
    vf_parts.append(
        f"drawtext=text='{safe_main}'"
        f"{fa}"
        f":fontsize=72:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.67"
        f":shadowcolor=black:shadowx=4:shadowy=4"
        f":enable='{enable}'"
    )

    # サブテキスト
    if subtext:
        safe_sub = _esc(subtext)
        vf_parts.append(
            f"drawtext=text='{safe_sub}'"
            f"{fa}"
            f":fontsize=46:fontcolor=white@0.85"
            f":x=(w-text_w)/2:y=h*0.80"
            f":shadowcolor=black:shadowx=2:shadowy=2"
            f":enable='{enable}'"
        )

    _apply_vf(input_path, output_path, vf_parts)
    print(f"   ✅ エンドカード追加（末尾 {duration:.0f}秒）: {output_path}")
    return output_path


# =============================================
# Step 5: サムネイル生成（YouTube 16:9 = 1280×720）
# =============================================

def generate_thumbnail(input_path: str, output_path: str,
                       title: str = "",
                       seek_ratio: float = 0.3) -> str:
    """
    YouTube サムネイル生成（✅ 自動化可能）
    - 1280×720 で最良フレームを抽出
    - Pillow でタイトルテキストをグラデーション背景付きで合成
    """
    print("\n🖼️  [サムネイル] 処理開始...")

    raw  = output_path.replace(".jpg", "_raw.jpg")
    dur  = get_duration(input_path)
    seek = dur * seek_ratio

    # 1280×720 でフレーム抽出（16:9 クロップ）
    run(
        f'{FFMPEG} -y -ss {seek:.2f} -i "{input_path}" '
        f'-vf "thumbnail=30,scale=1280:720:force_original_aspect_ratio=increase,'
        f'crop=1280:720" '
        f'-frames:v 1 "{raw}"'
    )

    if title:
        try:
            from PIL import Image, ImageDraw, ImageFont

            img = Image.open(raw).convert("RGB")
            w, h = img.size  # 1280×720

            # 下部グラデーション
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            grad_h = 250
            for y in range(h - grad_h, h):
                alpha = int(210 * (y - (h - grad_h)) / grad_h)
                od.rectangle([(0, y), (w, y)], fill=(0, 0, 0, alpha))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

            draw = ImageDraw.Draw(img)
            fp   = JAPANESE_FONT or None
            try:
                font = ImageFont.truetype(fp, 72) if fp else ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()

            lines = textwrap.wrap(title, width=18)[:2]
            y0    = h - len(lines) * 82 - 30
            for j, line in enumerate(lines):
                bbox   = draw.textbbox((0, 0), line, font=font)
                text_w = bbox[2] - bbox[0]
                x = (w - text_w) // 2
                y = y0 + j * 82
                draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 200))
                draw.text((x, y), line, font=font, fill=(255, 255, 255))

            img.save(output_path, "JPEG", quality=95)
            os.remove(raw)

        except ImportError:
            print("   ⚠️  Pillow未インストール → テキストなしで保存")
            os.rename(raw, output_path)
    else:
        os.rename(raw, output_path)

    size_kb = os.path.getsize(output_path) // 1024
    print(f"   ✅ サムネイル生成完了 (1280×720): {output_path} ({size_kb} KB)")
    return output_path


# =============================================
# Step 6: 縦型変換（ショート用・オプション）
# =============================================

def convert_to_vertical(input_path: str, output_path: str,
                        title: str = "") -> str:
    """
    9:16 縦型動画に変換（✅ 完全自動）
    ※ YouTubeショート用。run.py では convert_vertical=false がデフォルト。
    """
    print("\n📱 [縦型変換] 処理開始...")

    base_vf = (
        "scale=1080:1920:flags=lanczos:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    )

    vf = base_vf
    if title:
        safe = _esc(title)
        fa   = _font_arg()
        vf += (
            f",drawtext=text='{safe}'"
            f"{fa}"
            f":fontsize=52:fontcolor=white"
            f":x=(w-text_w)/2:y=80"
            f":shadowcolor=black:shadowx=3:shadowy=3"
        )

    run(f'{FFMPEG} -y -i "{input_path}" -vf "{vf}" -c:a copy "{output_path}"')
    print(f"   ✅ 縦型変換完了 (1080×1920): {output_path}")
    return output_path
