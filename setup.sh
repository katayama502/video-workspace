#!/bin/bash
# =============================================
# Video Gen & Editor - セットアップスクリプト
# Mac / Ubuntu / Cowork 対応
# =============================================

set -e

echo "🚀 Video Gen & Editor セットアップ開始"
echo "========================================"

# --- OS検出 ---
OS="$(uname -s)"

# --- ffmpeg ---
echo ""
echo "📦 [1/4] ffmpeg をインストール..."
if command -v ffmpeg &>/dev/null; then
    echo "  ✅ ffmpeg はすでにインストール済み ($(ffmpeg -version 2>&1 | head -1))"
else
    if [ "$OS" = "Darwin" ]; then
        brew install ffmpeg
    else
        sudo apt-get update -qq && sudo apt-get install -y ffmpeg fonts-noto-cjk
    fi
    echo "  ✅ ffmpeg インストール完了"
fi

# --- Python パッケージ ---
echo ""
echo "📦 [2/4] Python パッケージをインストール..."
pip install --quiet --upgrade \
    openai-whisper \
    torch \
    torchaudio \
    pillow \
    requests \
    python-dotenv \
    runwayml

echo "  ✅ Python パッケージ インストール完了"

# --- 日本語フォント（Mac用） ---
if [ "$OS" = "Darwin" ]; then
    echo ""
    echo "📦 [3/4] 日本語フォントをインストール..."
    if fc-list | grep -qi "noto"; then
        echo "  ✅ Noto フォントはすでに存在"
    else
        brew install --cask font-noto-sans-cjk-jp 2>/dev/null || \
        brew install font-noto-sans-cjk 2>/dev/null || \
        echo "  ⚠️  フォントの自動インストールをスキップ（手動で入れてください）"
    fi
else
    echo ""
    echo "📦 [3/4] 日本語フォント確認..."
    echo "  ✅ fonts-noto-cjk（apt でインストール済み）"
fi

# --- ディレクトリ構成 ---
echo ""
echo "📁 [4/4] ディレクトリ構成を作成..."
mkdir -p input output bgm assets

# .env ファイルの作成
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "⚠️  .env ファイルを作成しました。"
    echo "   KLING_ACCESS_KEY と KLING_SECRET_KEY を設定してください："
    echo "   $ nano .env"
else
    echo "  ✅ .env ファイルはすでに存在"
fi

echo ""
echo "========================================"
echo "✅ セットアップ完了！"
echo ""
echo "次のステップ："
echo "  1. .env にAPIキーを設定"
echo "  2. python run.py を実行"
echo "========================================"
