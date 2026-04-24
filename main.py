import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

AUDIO_FOLDER = "audio"

def transcribe(file_path):
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f
        )
    return result.text

def main():
    if not os.path.exists(AUDIO_FOLDER):
        print("audioフォルダがありません")
        return

    files = [f for f in os.listdir(AUDIO_FOLDER) if f.lower().endswith(".mp3")]

    if not files:
        print("audioフォルダにmp3ファイルがありません")
        return

    print("フォルダ内の音声を処理中...")

    for filename in files:
        file_path = os.path.join(AUDIO_FOLDER, filename)
        print(f"処理中: {filename}")

        text = transcribe(file_path)

        output_name = os.path.splitext(filename)[0] + ".txt"
        output_path = os.path.join(AUDIO_FOLDER, output_name)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(text)

    print("全部完了！")

if __name__ == "__main__":
    main()