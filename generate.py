"""
generate.py — Kling AI テキスト→動画生成モジュール
"""

import os
import time
import jwt
import requests
from dotenv import load_dotenv

load_dotenv()


# =============================================
# 認証ヘッダー生成
# =============================================

def _get_headers() -> dict:
    """Kling AI の JWT 認証ヘッダーを生成"""
    access_key = os.environ.get("KLING_ACCESS_KEY", "")
    secret_key = os.environ.get("KLING_SECRET_KEY", "")

    if not access_key or not secret_key:
        raise ValueError(
            "❌ KLING_ACCESS_KEY / KLING_SECRET_KEY が .env に設定されていません。\n"
            "   .env ファイルを開いてキーを設定してください。"
        )

    now = int(time.time())
    payload = {
        "iss": access_key,
        "exp": now + 1800,  # 30分有効
        "nbf": now - 5,
    }
    token = jwt.encode(payload, secret_key, algorithm="HS256")

    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


# =============================================
# テキスト → 動画生成
# =============================================

def generate_from_text(
    prompt: str,
    duration: int = 5,
    aspect_ratio: str = "16:9",
    mode: str = "std",
    output_path: str = "output/generated_clip.mp4",
) -> str:
    """
    テキストプロンプトから動画を生成（Kling AI text2video）

    Args:
        prompt       : 動画の内容を表す英語プロンプト（例: "a cat walking in a garden"）
        duration     : 動画の長さ（5 or 10 秒）※5秒=10クレジット
        aspect_ratio : "16:9"（横型/YouTube）or "9:16"（縦型/ショート）or "1:1"
        mode         : "std"（標準・無料枠節約）or "pro"（高品質）
        output_path  : 保存先パス

    Returns:
        出力MP4ファイルのパス
    """
    print(f"\n🎬 Kling AI 動画生成開始")
    print(f"   プロンプト : {prompt}")
    print(f"   長さ       : {duration}秒")
    print(f"   比率       : {aspect_ratio}")
    print(f"   モード     : {mode}")

    # --- タスク作成 ---
    resp = requests.post(
        "https://api.klingai.com/v1/videos/text2video",
        json={
            "model_name": "kling-v1",
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": aspect_ratio,
            "mode": mode,
        },
        headers=_get_headers(),
        timeout=30,
    )

    if resp.status_code != 200:
        raise Exception(f"APIエラー {resp.status_code}: {resp.text}")

    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"生成リクエスト失敗: {data}")

    task_id = data["data"]["task_id"]
    print(f"   タスクID   : {task_id}")
    print(f"   ⏳ 生成中（最大5分かかります）...\n")

    # --- ポーリング（完了待ち） ---
    for i in range(72):  # 最大6分（5秒×72回）
        time.sleep(5)
        result = requests.get(
            f"https://api.klingai.com/v1/videos/text2video/{task_id}",
            headers=_get_headers(),
            timeout=30,
        ).json()

        status = result["data"]["task_status"]
        elapsed = (i + 1) * 5
        print(f"   [{elapsed:>3}秒] ステータス: {status}", end="\r")

        if status == "succeed":
            print()
            video_url = result["data"]["task_result"]["videos"][0]["url"]
            break
        elif status == "failed":
            raise Exception(f"\n❌ 生成失敗: {result['data'].get('task_status_msg', '不明')}")
    else:
        raise TimeoutError("❌ タイムアウト: 6分以内に完了しませんでした")

    # --- ダウンロード ---
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    print(f"   📥 動画をダウンロード中...")
    r = requests.get(video_url, stream=True, timeout=60)
    r.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"   ✅ 生成完了: {output_path} ({size_mb:.1f} MB)")
    return output_path


# =============================================
# クレジット残量確認
# =============================================

def check_credits() -> dict:
    """残クレジットを確認して表示"""
    resp = requests.get(
        "https://api.klingai.com/v1/account/credits",
        headers=_get_headers(),
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json().get("data", {})
        remaining = data.get("remaining", "不明")
        print(f"💰 残クレジット: {remaining} （5秒動画1本=10クレジット）")
        return data
    else:
        print(f"⚠️  クレジット確認失敗: {resp.status_code}")
        return {}


# =============================================
# 単体テスト用エントリーポイント
# =============================================

if __name__ == "__main__":
    import sys

    prompt = sys.argv[1] if len(sys.argv) > 1 else "a peaceful mountain landscape with clouds moving slowly"
    aspect = sys.argv[2] if len(sys.argv) > 2 else "16:9"

    check_credits()
    generate_from_text(
        prompt=prompt,
        duration=5,
        aspect_ratio=aspect,
        output_path="output/generated_clip.mp4",
    )
